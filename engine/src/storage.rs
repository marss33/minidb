//! Tablas e indices guardados en B-trees.
//!
//! ```text
//! pagina 0   cabecera: "MINIDB02" + primera pagina libre (ver freelist.rs)
//! pagina 1   raiz del catalogo
//! resto      nodos de los B-trees o paginas libres
//! ```
//!
//! Todo es un B-tree (ver btree.rs):
//!
//! - el catalogo: clave = nombre, valor = (tipo, tabla, sql, raiz, columna)
//! - cada tabla: clave = rowid, valor = la fila (ver record.rs)
//! - cada indice: clave = valor de la columna + rowid, valor vacio (ver keys.rs)
//!
//! El rowid es un numero interno que identifica cada fila; Python lo usa para
//! decir que filas cambiar o borrar. Los indices se actualizan solos cada vez que
//! cambia una fila de su tabla.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::ops::Bound;
use std::path::Path;

use crate::btree::{self, MAX_CELL, MAX_KEY};
use crate::error::{Error, Result, corrupt};
use crate::keys;
use crate::pager::{self, PageId, Pager};
use crate::record::{self, Row, Value};

const MAGIC: &[u8; 8] = b"MINIDB02";
const OLD_MAGIC: &[u8; 8] = b"MINIDB01";
const CATALOG_ROOT: PageId = 1;

/// Lo maximo que puede ocupar una fila codificada (la clave de una tabla mide 8).
pub const MAX_ROW: usize = MAX_CELL - 4 - 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Table,
    Index,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Table => "table",
            Kind::Index => "index",
        }
    }
}

/// Una tabla o un indice, como lo ve el que usa el motor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub kind: Kind,
    pub name: String,
    pub table: String,
    pub sql: String,
}

/// Uno de los extremos de una busqueda por indice: el valor y si se incluye.
pub type Limit = Option<(Value, bool)>;

struct TableInfo {
    sql: String,
    root: PageId,
    /// Se calcula la primera vez que hace falta.
    next_rowid: Option<i64>,
}

struct IndexInfo {
    table: String,
    sql: String,
    root: PageId,
    column: usize,
}

pub struct Storage {
    pager: Pager,
    tables: BTreeMap<String, TableInfo>,
    indexes: BTreeMap<String, IndexInfo>,
}

impl Storage {
    /// Abre la base guardada en `path`, o la crea si el archivo no existe o esta vacio.
    pub fn open(path: &Path) -> Result<Storage> {
        // primero el journal: un commit cortado puede haber dejado el archivo a medias
        pager::recover(path)?;
        check_magic(path)?;
        let mut pager = Pager::open(path)?;
        if pager.page_count() == 0 {
            let header = pager.append()?;
            pager.write(header)?[..MAGIC.len()].copy_from_slice(MAGIC);
            let catalog = pager.append()?;
            btree::init(&mut pager, catalog)?;
            pager.flush()?;
        } else if pager.page_count() < 2 {
            return corrupt("falta la pagina del catalogo");
        }

        let mut storage = Storage {
            pager,
            tables: BTreeMap::new(),
            indexes: BTreeMap::new(),
        };
        storage.load_catalog()?;
        Ok(storage)
    }

    /// Las tablas y los indices, ordenados por nombre.
    pub fn catalog(&self) -> Vec<Entry> {
        let tables = self.tables.iter().map(|(name, t)| Entry {
            kind: Kind::Table,
            name: name.clone(),
            table: name.clone(),
            sql: t.sql.clone(),
        });
        let indexes = self.indexes.iter().map(|(name, i)| Entry {
            kind: Kind::Index,
            name: name.clone(),
            table: i.table.clone(),
            sql: i.sql.clone(),
        });
        tables.chain(indexes).collect()
    }

    pub fn create_table(&mut self, name: &str, sql: &str) -> Result<()> {
        self.check_free_name(name)?;
        let root = btree::create(&mut self.pager)?;
        self.save_in_catalog(name, Kind::Table, name, sql, root, None)?;
        self.tables.insert(
            name.into(),
            TableInfo {
                sql: sql.into(),
                root,
                next_rowid: None,
            },
        );
        Ok(())
    }

    /// Crea un indice sobre la columna numero `column` y lo llena con las filas que ya hay.
    pub fn create_index(
        &mut self,
        name: &str,
        table: &str,
        column: usize,
        sql: &str,
    ) -> Result<()> {
        self.check_free_name(name)?;
        let table_root = self.table(table)?.root;
        let root = btree::create(&mut self.pager)?;
        for (rowid, row) in self.read_rows(table_root)? {
            let key = index_key(name, &row, column, rowid)?;
            btree::put(&mut self.pager, root, &key, &[])?;
        }
        self.save_in_catalog(name, Kind::Index, table, sql, root, Some(column))?;
        self.indexes.insert(
            name.into(),
            IndexInfo {
                table: table.into(),
                sql: sql.into(),
                root,
                column,
            },
        );
        Ok(())
    }

    /// Borra la tabla y todos sus indices.
    pub fn drop_table(&mut self, name: &str) -> Result<()> {
        let root = self.table(name)?.root;
        let indexes: Vec<String> = self
            .indexes
            .iter()
            .filter(|(_, i)| i.table == name)
            .map(|(n, _)| n.clone())
            .collect();
        for index in indexes {
            self.drop_index(&index)?;
        }
        btree::destroy(&mut self.pager, root)?;
        btree::delete(&mut self.pager, CATALOG_ROOT, name.as_bytes())?;
        self.tables.remove(name);
        Ok(())
    }

    pub fn drop_index(&mut self, name: &str) -> Result<()> {
        let Some(index) = self.indexes.get(name) else {
            return Err(Error::Invalid(format!("no existe el indice {name}")));
        };
        btree::destroy(&mut self.pager, index.root)?;
        btree::delete(&mut self.pager, CATALOG_ROOT, name.as_bytes())?;
        self.indexes.remove(name);
        Ok(())
    }

    /// Agrega las filas y devuelve el rowid que le toco a cada una.
    pub fn insert(&mut self, table: &str, rows: &[Row]) -> Result<Vec<i64>> {
        let root = self.table(table)?.root;
        let indexes = self.indexes_of(table);
        let mut next = self.next_rowid(table)?;
        let mut rowids = Vec::with_capacity(rows.len());
        for row in rows {
            let rowid = next;
            next = next
                .checked_add(1)
                .ok_or_else(|| Error::Invalid("no hay mas rowids".into()))?;
            btree::put(
                &mut self.pager,
                root,
                &keys::rowid(rowid),
                &encode_row(row)?,
            )?;
            for (name, index_root, column) in &indexes {
                let key = index_key(name, row, *column, rowid)?;
                btree::put(&mut self.pager, *index_root, &key, &[])?;
            }
            rowids.push(rowid);
        }
        self.tables.get_mut(table).expect("existe").next_rowid = Some(next);
        Ok(rowids)
    }

    /// Todas las filas, ordenadas por rowid.
    pub fn scan(&mut self, table: &str) -> Result<Vec<(i64, Row)>> {
        let root = self.table(table)?.root;
        self.read_rows(root)
    }

    /// Las filas cuyo valor en la columna del indice esta entre `low` y `high`,
    /// ordenadas por rowid. Las filas con NULL en esa columna nunca aparecen.
    pub fn search(&mut self, index: &str, low: &Limit, high: &Limit) -> Result<Vec<(i64, Row)>> {
        let Some(info) = self.indexes.get(index) else {
            return Err(Error::Invalid(format!("no existe el indice {index}")));
        };
        let index_root = info.root;
        let table_root = self.table(&info.table)?.root;

        let start = match low {
            None => keys::FIRST_NOT_NULL.to_vec(),
            Some((value, true)) => keys::value(value),
            Some((value, false)) => [keys::value(value), keys::AFTER_VALUE.to_vec()].concat(),
        };
        let end = high.as_ref().map(|(value, inclusive)| {
            let mut key = keys::value(value);
            if *inclusive {
                key.extend_from_slice(&keys::AFTER_VALUE);
            }
            key
        });
        let end = match &end {
            Some(key) => Bound::Excluded(key.as_slice()),
            None => Bound::Unbounded,
        };

        let mut rowids = Vec::new();
        btree::range(
            &mut self.pager,
            index_root,
            Bound::Included(&start),
            end,
            |key, _| {
                rowids.push(keys::rowid_of_index_entry(key)?);
                Ok(())
            },
        )?;
        rowids.sort_unstable();

        let mut rows = Vec::with_capacity(rowids.len());
        for rowid in rowids {
            match self.get_row(table_root, rowid)? {
                Some(row) => rows.push((rowid, row)),
                None => {
                    return corrupt(format!("el indice {index} apunta a una fila que no existe"));
                }
            }
        }
        Ok(rows)
    }

    /// Reemplaza filas existentes.
    pub fn update(&mut self, table: &str, changes: &[(i64, Row)]) -> Result<()> {
        let root = self.table(table)?.root;
        let indexes = self.indexes_of(table);
        for (rowid, row) in changes {
            let Some(old) = self.get_row(root, *rowid)? else {
                return Err(Error::Invalid(format!("no existe la fila {rowid}")));
            };
            btree::put(
                &mut self.pager,
                root,
                &keys::rowid(*rowid),
                &encode_row(row)?,
            )?;
            for (name, index_root, column) in &indexes {
                if old.get(*column) != row.get(*column) {
                    let old_key = index_key(name, &old, *column, *rowid)?;
                    btree::delete(&mut self.pager, *index_root, &old_key)?;
                    let new_key = index_key(name, row, *column, *rowid)?;
                    btree::put(&mut self.pager, *index_root, &new_key, &[])?;
                }
            }
        }
        Ok(())
    }

    pub fn delete(&mut self, table: &str, rowids: &[i64]) -> Result<()> {
        let root = self.table(table)?.root;
        let indexes = self.indexes_of(table);
        for &rowid in rowids {
            let Some(old) = btree::delete(&mut self.pager, root, &keys::rowid(rowid))? else {
                continue;
            };
            let old = decode_row(&old)?;
            for (name, index_root, column) in &indexes {
                let key = index_key(name, &old, *column, rowid)?;
                btree::delete(&mut self.pager, *index_root, &key)?;
            }
        }
        Ok(())
    }

    /// Borra todas las filas de la tabla (y de sus indices) y libera sus paginas.
    pub fn clear(&mut self, table: &str) -> Result<()> {
        let root = self.table(table)?.root;
        btree::clear(&mut self.pager, root)?;
        for (_, index_root, _) in self.indexes_of(table) {
            btree::clear(&mut self.pager, index_root)?;
        }
        self.tables.get_mut(table).expect("existe").next_rowid = None;
        Ok(())
    }

    /// Guarda en el archivo todos los cambios hechos desde el ultimo commit.
    pub fn commit(&mut self) -> Result<()> {
        self.pager.flush()
    }

    /// Descarta todos los cambios hechos desde el ultimo commit.
    pub fn rollback(&mut self) -> Result<()> {
        self.pager.rollback();
        self.load_catalog()
    }

    pub fn page_count(&self) -> u32 {
        self.pager.page_count()
    }

    // ---- ayudas ----

    fn table(&self, name: &str) -> Result<&TableInfo> {
        self.tables
            .get(name)
            .ok_or_else(|| Error::Invalid(format!("no existe la tabla {name}")))
    }

    fn check_free_name(&self, name: &str) -> Result<()> {
        if self.tables.contains_key(name) || self.indexes.contains_key(name) {
            return Err(Error::Invalid(format!(
                "ya existe una tabla o indice llamado {name}"
            )));
        }
        Ok(())
    }

    /// (nombre, raiz, columna) de cada indice de la tabla.
    fn indexes_of(&self, table: &str) -> Vec<(String, PageId, usize)> {
        self.indexes
            .iter()
            .filter(|(_, i)| i.table == table)
            .map(|(name, i)| (name.clone(), i.root, i.column))
            .collect()
    }

    fn next_rowid(&mut self, table: &str) -> Result<i64> {
        let info = self.table(table)?;
        if let Some(next) = info.next_rowid {
            return Ok(next);
        }
        let root = info.root;
        Ok(match btree::last_key(&mut self.pager, root)? {
            Some(key) => keys::decode_rowid(&key)? + 1,
            None => 1,
        })
    }

    fn read_rows(&mut self, root: PageId) -> Result<Vec<(i64, Row)>> {
        let mut rows = Vec::new();
        btree::range(
            &mut self.pager,
            root,
            Bound::Unbounded,
            Bound::Unbounded,
            |key, value| {
                rows.push((keys::decode_rowid(key)?, decode_row(value)?));
                Ok(())
            },
        )?;
        Ok(rows)
    }

    fn get_row(&mut self, root: PageId, rowid: i64) -> Result<Option<Row>> {
        btree::get(&mut self.pager, root, &keys::rowid(rowid))?
            .map(|bytes| decode_row(&bytes))
            .transpose()
    }

    fn save_in_catalog(
        &mut self,
        name: &str,
        kind: Kind,
        table: &str,
        sql: &str,
        root: PageId,
        column: Option<usize>,
    ) -> Result<()> {
        let column = column.map_or(-1, |c| c as i64);
        let row = [
            Value::Text(kind.as_str().into()),
            Value::Text(table.into()),
            Value::Text(sql.into()),
            Value::Int(root.into()),
            Value::Int(column),
        ];
        let value = record::encode(&row)?;
        if name.len() > MAX_KEY || value.len() + name.len() + 4 > MAX_CELL {
            return Err(Error::Invalid(format!(
                "la definicion de {name} es demasiado larga"
            )));
        }
        btree::put(&mut self.pager, CATALOG_ROOT, name.as_bytes(), &value)
    }

    fn load_catalog(&mut self) -> Result<()> {
        let mut entries = Vec::new();
        btree::range(
            &mut self.pager,
            CATALOG_ROOT,
            Bound::Unbounded,
            Bound::Unbounded,
            |k, v| {
                entries.push((k.to_vec(), v.to_vec()));
                Ok(())
            },
        )?;

        self.tables.clear();
        self.indexes.clear();
        for (key, value) in entries {
            let Ok(name) = String::from_utf8(key) else {
                return corrupt("un nombre del catalogo no es utf-8");
            };
            let (kind, table, sql, root, column) = match <[Value; 5]>::try_from(decode_row(&value)?)
            {
                Ok(
                    [
                        Value::Text(k),
                        Value::Text(t),
                        Value::Text(s),
                        Value::Int(r),
                        Value::Int(c),
                    ],
                ) => (k, t, s, r, c),
                _ => {
                    return corrupt(format!(
                        "la entrada del catalogo de {name} tiene otro formato"
                    ));
                }
            };
            let root = match PageId::try_from(root) {
                Ok(id) if id > CATALOG_ROOT && id < self.pager.page_count() => id,
                _ => return corrupt(format!("{name} empieza en una pagina invalida")),
            };
            match (kind.as_str(), usize::try_from(column)) {
                ("table", _) => {
                    let info = TableInfo {
                        sql,
                        root,
                        next_rowid: None,
                    };
                    self.tables.insert(name, info);
                }
                ("index", Ok(column)) => {
                    let info = IndexInfo {
                        table,
                        sql,
                        root,
                        column,
                    };
                    self.indexes.insert(name, info);
                }
                _ => return corrupt(format!("la entrada del catalogo de {name} es invalida")),
            }
        }
        if let Some((name, _)) = self
            .indexes
            .iter()
            .find(|(_, i)| !self.tables.contains_key(&i.table))
        {
            return corrupt(format!("el indice {name} es de una tabla que no existe"));
        }
        Ok(())
    }
}

fn check_magic(path: &Path) -> Result<()> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    if file.metadata()?.len() == 0 {
        return Ok(());
    }
    let mut magic = [0; MAGIC.len()];
    match file.read_exact(&mut magic) {
        Ok(()) if &magic == MAGIC => Ok(()),
        Ok(()) if &magic == OLD_MAGIC => Err(Error::Invalid(
            "el archivo es de una version vieja de minidb (sin B-trees); hay que crearlo de nuevo"
                .into(),
        )),
        _ => Err(Error::NotADatabase),
    }
}

fn encode_row(row: &[Value]) -> Result<Vec<u8>> {
    let bytes = record::encode(row)?;
    if bytes.len() > MAX_ROW {
        return Err(Error::Invalid(format!(
            "la fila ocupa {} bytes y el maximo es {MAX_ROW}",
            bytes.len()
        )));
    }
    Ok(bytes)
}

fn decode_row(bytes: &[u8]) -> Result<Row> {
    let mut pos = 0;
    let row = record::decode(bytes, &mut pos)?;
    if pos != bytes.len() {
        return corrupt("sobran bytes despues de una fila");
    }
    Ok(row)
}

fn index_key(index: &str, row: &[Value], column: usize, rowid: i64) -> Result<Vec<u8>> {
    let Some(value) = row.get(column) else {
        return corrupt(format!("una fila no tiene la columna del indice {index}"));
    };
    let key = keys::index_entry(value, rowid);
    if key.len() > MAX_KEY {
        return Err(Error::Invalid(format!(
            "un valor es demasiado largo para el indice {index} (ocupa {} bytes y el maximo es {})",
            key.len() - 8,
            MAX_KEY - 8
        )));
    }
    Ok(key)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pager::journal_path;
    use crate::test_util::{Rng, TempFile};

    fn int(n: i64) -> Value {
        Value::Int(n)
    }

    fn text(s: &str) -> Value {
        Value::Text(s.into())
    }

    fn names(storage: &Storage) -> Vec<String> {
        storage.catalog().into_iter().map(|e| e.name).collect()
    }

    fn rows(pairs: Vec<(i64, Row)>) -> Vec<Row> {
        pairs.into_iter().map(|(_, row)| row).collect()
    }

    /// Una tabla t (n INT, s TEXT) con un indice t_n sobre n.
    fn with_table(tmp: &TempFile) -> Storage {
        let mut s = Storage::open(tmp.path()).unwrap();
        s.create_table("t", "CREATE TABLE t (n INT, s TEXT)")
            .unwrap();
        s.create_index("t_n", "t", 0, "CREATE INDEX t_n ON t (n)")
            .unwrap();
        s
    }

    #[test]
    fn crear_insertar_y_reabrir() {
        let tmp = TempFile::new();
        let data = vec![vec![int(1), text("Ana")], vec![int(2), Value::Null]];
        {
            let mut s = Storage::open(tmp.path()).unwrap();
            s.create_table("t", "CREATE TABLE t (id INT, nombre TEXT)")
                .unwrap();
            assert_eq!(s.insert("t", &data).unwrap(), [1, 2]);
            s.commit().unwrap();
        }
        let mut s = Storage::open(tmp.path()).unwrap();
        assert_eq!(
            s.catalog(),
            [Entry {
                kind: Kind::Table,
                name: "t".into(),
                table: "t".into(),
                sql: "CREATE TABLE t (id INT, nombre TEXT)".into(),
            }]
        );
        assert_eq!(
            s.scan("t").unwrap(),
            [(1, data[0].clone()), (2, data[1].clone())]
        );
        // los rowids siguen desde donde quedaron
        assert_eq!(s.insert("t", &[vec![int(3), text("x")]]).unwrap(), [3]);
    }

    #[test]
    fn sin_commit_no_queda_nada() {
        let tmp = TempFile::new();
        {
            let mut s = Storage::open(tmp.path()).unwrap();
            s.create_table("t", "x").unwrap();
        }
        assert!(names(&Storage::open(tmp.path()).unwrap()).is_empty());
    }

    #[test]
    fn rollback_descarta_la_sentencia() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        s.insert("t", &[vec![int(1), text("a")]]).unwrap();
        s.commit().unwrap();

        s.insert("t", &[vec![int(2), text("b")]]).unwrap();
        s.create_table("u", "y").unwrap();
        s.drop_index("t_n").unwrap();
        s.rollback().unwrap();

        assert_eq!(names(&s), ["t", "t_n"]);
        assert_eq!(rows(s.scan("t").unwrap()), [vec![int(1), text("a")]]);
        assert_eq!(rows(s.search("t_n", &None, &None).unwrap()).len(), 1);
        assert_eq!(s.insert("t", &[vec![int(3), Value::Null]]).unwrap(), [2]);
    }

    #[test]
    fn update_y_delete_mantienen_los_indices() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        let data: Vec<Row> = (0..1000)
            .map(|i| vec![int(i % 10), text(&format!("fila {i}"))])
            .collect();
        let ids = s.insert("t", &data).unwrap();

        let eq = |v: i64| Some((int(v), true));
        assert_eq!(s.search("t_n", &eq(3), &eq(3)).unwrap().len(), 100);

        // las filas con n = 3 pasan a tener n = 30
        let changes: Vec<(i64, Row)> = s
            .search("t_n", &eq(3), &eq(3))
            .unwrap()
            .into_iter()
            .map(|(id, row)| (id, vec![int(30), row[1].clone()]))
            .collect();
        s.update("t", &changes).unwrap();
        assert!(s.search("t_n", &eq(3), &eq(3)).unwrap().is_empty());
        assert_eq!(s.search("t_n", &eq(30), &eq(30)).unwrap().len(), 100);

        // se borran las filas pares
        let even: Vec<i64> = ids.iter().copied().filter(|id| id % 2 == 0).collect();
        s.delete("t", &even).unwrap();
        assert_eq!(s.scan("t").unwrap().len(), 500);
        assert_eq!(s.search("t_n", &None, &None).unwrap().len(), 500);
        // n = 30 venia de i % 10 = 3, que son todas filas con rowid par (i impar)
        assert!(s.search("t_n", &eq(30), &eq(30)).unwrap().is_empty());
    }

    #[test]
    fn busquedas_por_rango() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        let data: Vec<Row> = [5, -2, 9, 0, 5, 7, 3]
            .into_iter()
            .map(|n| vec![int(n), Value::Null])
            .chain([vec![Value::Null, text("sin n")]])
            .collect();
        s.insert("t", &data).unwrap();

        let mut find = |low: Limit, high: Limit| -> Vec<i64> {
            let mut found: Vec<i64> = s
                .search("t_n", &low, &high)
                .unwrap()
                .into_iter()
                .map(|(_, row)| match row[0] {
                    Value::Int(n) => n,
                    _ => panic!("un NULL en una busqueda"),
                })
                .collect();
            found.sort();
            found
        };
        assert_eq!(find(None, None), [-2, 0, 3, 5, 5, 7, 9]);
        assert_eq!(find(Some((int(5), true)), Some((int(5), true))), [5, 5]);
        assert_eq!(find(Some((int(3), false)), Some((int(7), false))), [5, 5]);
        assert_eq!(
            find(Some((int(3), true)), Some((int(7), true))),
            [3, 5, 5, 7]
        );
        assert_eq!(find(None, Some((int(0), true))), [-2, 0]);
        assert_eq!(find(Some((int(7), false)), None), [9]);
        assert!(find(Some((int(8), true)), Some((int(6), true))).is_empty());
    }

    #[test]
    fn indice_sobre_texto() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        s.create_index("t_s", "t", 1, "CREATE INDEX t_s ON t (s)")
            .unwrap();
        let words = ["pera", "banana", "manzana", "Banana", "pera", "", "a\0b"];
        let data: Vec<Row> = words.iter().map(|w| vec![Value::Null, text(w)]).collect();
        s.insert("t", &data).unwrap();
        let found = |s: &mut Storage, low: Limit, high: Limit| -> Vec<Row> {
            rows(s.search("t_s", &low, &high).unwrap())
        };
        assert_eq!(
            found(
                &mut s,
                Some((text("pera"), true)),
                Some((text("pera"), true))
            )
            .len(),
            2
        );
        // 'b' <= s < 'n': banana y manzana
        assert_eq!(
            found(&mut s, Some((text("b"), true)), Some((text("n"), false))),
            [
                vec![Value::Null, text("banana")],
                vec![Value::Null, text("manzana")]
            ]
        );
    }

    #[test]
    fn create_index_indexa_las_filas_que_ya_estaban() {
        let tmp = TempFile::new();
        {
            let mut s = Storage::open(tmp.path()).unwrap();
            s.create_table("t", "x").unwrap();
            let data: Vec<Row> = (0..3000).map(|i| vec![int(i)]).collect();
            s.insert("t", &data).unwrap();
            s.create_index("i", "t", 0, "y").unwrap();
            s.commit().unwrap();
        }
        let mut s = Storage::open(tmp.path()).unwrap();
        let found = s.search("i", &Some((int(2500), true)), &None).unwrap();
        assert_eq!(found.len(), 500);
        assert_eq!(found[0].1, [int(2500)]);
    }

    #[test]
    fn drop_table_libera_todo() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        let data: Vec<Row> = (0..3000)
            .map(|i| vec![int(i), text("relleno relleno")])
            .collect();
        s.insert("t", &data).unwrap();
        s.commit().unwrap();
        let pages = s.page_count();

        s.drop_table("t").unwrap();
        assert!(names(&s).is_empty(), "el indice se borra con la tabla");
        let mut s2 = s;
        s2.create_table("u", "x").unwrap();
        s2.create_index("u_n", "u", 0, "y").unwrap();
        s2.insert("u", &data).unwrap();
        assert_eq!(s2.page_count(), pages, "u usa las paginas que dejo t");
        s2.commit().unwrap();
        drop(s2);
        assert_eq!(names(&Storage::open(tmp.path()).unwrap()), ["u", "u_n"]);
    }

    #[test]
    fn clear_vacia_la_tabla_y_sus_indices() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        let data: Vec<Row> = (0..2000).map(|i| vec![int(i), Value::Null]).collect();
        s.insert("t", &data).unwrap();
        let pages = s.page_count();
        s.clear("t").unwrap();
        assert!(s.scan("t").unwrap().is_empty());
        assert!(s.search("t_n", &None, &None).unwrap().is_empty());
        assert_eq!(
            s.insert("t", &data).unwrap()[0],
            1,
            "los rowids vuelven a empezar"
        );
        assert_eq!(s.page_count(), pages);
    }

    #[test]
    fn errores() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        assert!(matches!(s.create_table("t", "x"), Err(Error::Invalid(_))));
        assert!(matches!(s.create_table("t_n", "x"), Err(Error::Invalid(_))));
        assert!(matches!(s.scan("nada"), Err(Error::Invalid(_))));
        assert!(matches!(s.drop_table("nada"), Err(Error::Invalid(_))));
        assert!(matches!(s.drop_index("nada"), Err(Error::Invalid(_))));
        assert!(matches!(
            s.create_index("i", "nada", 0, "x"),
            Err(Error::Invalid(_))
        ));

        let big = vec![int(1), text(&"x".repeat(MAX_ROW))];
        assert!(matches!(s.insert("t", &[big]), Err(Error::Invalid(_))));
    }

    #[test]
    fn la_fila_mas_grande_que_entra() {
        let tmp = TempFile::new();
        let mut s = Storage::open(tmp.path()).unwrap();
        s.create_table("t", "x").unwrap();
        // 2 (cantidad) + 1 (tag) + 4 (largo) + texto = MAX_ROW
        let row = vec![text(&"x".repeat(MAX_ROW - 7))];
        s.insert("t", &[row.clone(), row.clone(), row.clone()])
            .unwrap();
        assert_eq!(rows(s.scan("t").unwrap()), [row.clone(), row.clone(), row]);
    }

    #[test]
    fn valor_demasiado_largo_para_un_indice() {
        let tmp = TempFile::new();
        let mut s = with_table(&tmp);
        s.create_index("t_s", "t", 1, "y").unwrap();
        let err = s
            .insert("t", &[vec![int(1), text(&"x".repeat(2000))]])
            .unwrap_err();
        assert!(err.to_string().contains("indice t_s"), "{err}");
    }

    #[test]
    fn corte_de_luz_en_el_commit() {
        let tmp = TempFile::new();
        let before: Vec<Row> = (0..500).map(|i| vec![int(i), text("antes")]).collect();
        {
            let mut s = with_table(&tmp);
            s.insert("t", &before).unwrap();
            s.commit().unwrap();

            let changes: Vec<(i64, Row)> = s
                .scan("t")
                .unwrap()
                .into_iter()
                .map(|(id, _)| (id, vec![int(-1), text("despues")]))
                .collect();
            s.update("t", &changes).unwrap();
            s.create_table("otra", "x").unwrap();
            s.pager.crash_after = Some(3);
            assert!(s.commit().is_err());
        }
        assert!(journal_path(tmp.path()).exists());

        let mut s = Storage::open(tmp.path()).unwrap();
        assert_eq!(names(&s), ["t", "t_n"]);
        assert_eq!(rows(s.scan("t").unwrap()), before);
        assert!(
            s.search("t_n", &Some((int(-1), true)), &Some((int(-1), true)))
                .unwrap()
                .is_empty()
        );
    }

    /// Muchas operaciones al azar, comparando contra un modelo en memoria y
    /// reabriendo el archivo cada tanto.
    #[test]
    fn operaciones_al_azar_contra_un_modelo() {
        let tmp = TempFile::new();
        let mut model: BTreeMap<i64, Row> = BTreeMap::new();
        let mut rng = Rng::new(3);
        let mut s = with_table(&tmp);

        for round in 0..40 {
            for _ in 0..50 {
                match rng.below(4) {
                    0 | 1 => {
                        let rows: Vec<Row> = (0..1 + rng.below(20))
                            .map(|_| {
                                vec![int(rng.below(50) as i64), text(&"z".repeat(rng.below(300)))]
                            })
                            .collect();
                        let ids = s.insert("t", &rows).unwrap();
                        model.extend(ids.into_iter().zip(rows));
                    }
                    2 => {
                        let ids: Vec<i64> = model
                            .keys()
                            .copied()
                            .filter(|_| rng.below(5) == 0)
                            .collect();
                        let changes: Vec<(i64, Row)> = ids
                            .iter()
                            .map(|&id| (id, vec![int(rng.below(50) as i64), text("cambiada")]))
                            .collect();
                        s.update("t", &changes).unwrap();
                        model.extend(changes);
                    }
                    _ => {
                        let ids: Vec<i64> = model
                            .keys()
                            .copied()
                            .filter(|_| rng.below(4) == 0)
                            .collect();
                        s.delete("t", &ids).unwrap();
                        for id in ids {
                            model.remove(&id);
                        }
                    }
                }
            }
            s.commit().unwrap();
            if round % 10 == 9 {
                drop(s);
                s = Storage::open(tmp.path()).unwrap();
            }

            let expected: Vec<(i64, Row)> = model.clone().into_iter().collect();
            assert_eq!(s.scan("t").unwrap(), expected);
            let n = rng.below(50) as i64;
            let by_index = s
                .search("t_n", &Some((int(n), true)), &Some((int(n), true)))
                .unwrap();
            let by_model: Vec<(i64, Row)> = expected
                .into_iter()
                .filter(|(_, row)| row[0] == int(n))
                .collect();
            assert_eq!(by_index, by_model);
        }
    }

    #[test]
    fn archivo_que_no_es_de_minidb() {
        let tmp = TempFile::new();
        std::fs::write(tmp.path(), "CREATE TABLE t (a INT);").unwrap();
        assert!(matches!(
            Storage::open(tmp.path()),
            Err(Error::NotADatabase)
        ));
    }

    #[test]
    fn archivo_del_formato_viejo() {
        let tmp = TempFile::new();
        let mut bytes = OLD_MAGIC.to_vec();
        bytes.resize(2 * crate::pager::PAGE_SIZE, 0);
        std::fs::write(tmp.path(), bytes).unwrap();
        let err = Storage::open(tmp.path()).err().unwrap();
        assert!(err.to_string().contains("version vieja"), "{err}");
    }
}
