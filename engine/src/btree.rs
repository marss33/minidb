//! B+tree con claves y valores de bytes.
//!
//! Los datos estan solo en las hojas, ordenados por clave. Los nodos internos
//! solo guian la busqueda: `entries[i] = (clave, hijo)` manda a `hijo` las
//! claves >= `clave`, y `first` recibe las menores que la primera clave.
//!
//! ```text
//! hoja     tipo 1 (u8) | cantidad (u16) | 0 (u32, sin uso)
//!          | por celda: largo clave (u16) | largo valor (u16) | clave | valor
//! interno  tipo 2 (u8) | cantidad (u16) | primer hijo (u32)
//!          | por entrada: largo clave (u16) | clave | hijo (u32)
//! ```
//!
//! Para que el codigo sea simple, cada operacion lee el nodo entero, lo cambia en
//! memoria y lo vuelve a escribir.
//!
//! - Cuando un nodo no entra en su pagina se parte en dos y la clave del medio
//!   sube al padre. La raiz nunca cambia de pagina: cuando se parte, su contenido
//!   se muda a una pagina nueva y ella pasa a ser un nodo interno con dos hijos.
//!   Asi el catalogo puede guardar la raiz de cada tabla sin tener que actualizarla.
//! - Cuando un nodo queda con menos de un cuarto de pagina se junta con un vecino,
//!   si entre los dos entran en una pagina, y la pagina que sobra se libera. Si la
//!   raiz queda con un solo hijo, el hijo pasa a ser la raiz.
//! - Los rangos se recorren bajando por el arbol, sin punteros entre hojas: asi
//!   sacar una hoja no obliga a arreglar a la anterior.

use std::collections::HashSet;
use std::ops::Bound;

use crate::error::{Error, Result, corrupt};
use crate::freelist::{self, read_u32, write_u32};
use crate::pager::{PAGE_SIZE, PageId, Pager};

const LEAF: u8 = 1;
const INTERNAL: u8 = 2;
const NODE_HEADER: usize = 7;

/// Lo maximo que puede ocupar una celda de una hoja (clave + valor + 4).
/// Con celdas de hasta media pagina, partir un nodo en dos siempre alcanza.
pub const MAX_CELL: usize = (PAGE_SIZE - NODE_HEADER) / 2;
/// Lo maximo que puede medir una clave, para que un nodo interno tenga varios hijos.
pub const MAX_KEY: usize = 1024;
const MAX_DEPTH: usize = 32;

enum Node {
    Leaf {
        cells: Vec<(Vec<u8>, Vec<u8>)>,
    },
    Internal {
        first: PageId,
        entries: Vec<(Vec<u8>, PageId)>,
    },
}

fn leaf_cell_size(key: &[u8], value: &[u8]) -> usize {
    4 + key.len() + value.len()
}

fn entry_size(key: &[u8]) -> usize {
    6 + key.len()
}

impl Node {
    fn size(&self) -> usize {
        NODE_HEADER
            + match self {
                Node::Leaf { cells } => cells
                    .iter()
                    .map(|(k, v)| leaf_cell_size(k, v))
                    .sum::<usize>(),
                Node::Internal { entries, .. } => {
                    entries.iter().map(|(k, _)| entry_size(k)).sum::<usize>()
                }
            }
    }

    fn read(pager: &mut Pager, id: PageId) -> Result<Node> {
        let page = pager.read(id)?;
        let count = u16::from_le_bytes([page[1], page[2]]);
        let link = read_u32(page, 3);
        let mut reader = Reader {
            page,
            pos: NODE_HEADER,
            id,
        };
        match page[0] {
            LEAF => {
                let mut cells = Vec::with_capacity(count.into());
                for _ in 0..count {
                    let key_len = reader.u16()?;
                    let value_len = reader.u16()?;
                    let key = reader.bytes(key_len)?.to_vec();
                    let value = reader.bytes(value_len)?.to_vec();
                    cells.push((key, value));
                }
                Ok(Node::Leaf { cells })
            }
            INTERNAL => {
                let mut entries = Vec::with_capacity(count.into());
                for _ in 0..count {
                    let key_len = reader.u16()?;
                    let key = reader.bytes(key_len)?.to_vec();
                    let child = reader.u32()?;
                    entries.push((key, child));
                }
                Ok(Node::Internal {
                    first: link,
                    entries,
                })
            }
            other => corrupt(format!("la pagina {id} no es un nodo (tipo {other})")),
        }
    }

    fn write(&self, pager: &mut Pager, id: PageId) -> Result<()> {
        debug_assert!(self.size() <= PAGE_SIZE);
        let page = pager.write(id)?;
        page.fill(0);
        let mut pos = NODE_HEADER;
        let mut put = |bytes: &[u8]| {
            page[pos..pos + bytes.len()].copy_from_slice(bytes);
            pos += bytes.len();
        };
        let (kind, count, link) = match self {
            Node::Leaf { cells } => {
                for (key, value) in cells {
                    put(&(key.len() as u16).to_le_bytes());
                    put(&(value.len() as u16).to_le_bytes());
                    put(key);
                    put(value);
                }
                (LEAF, cells.len(), 0)
            }
            Node::Internal { first, entries } => {
                for (key, child) in entries {
                    put(&(key.len() as u16).to_le_bytes());
                    put(key);
                    put(&child.to_le_bytes());
                }
                (INTERNAL, entries.len(), *first)
            }
        };
        page[0] = kind;
        page[1..3].copy_from_slice(&(count as u16).to_le_bytes());
        write_u32(page, 3, link);
        Ok(())
    }
}

struct Reader<'a> {
    page: &'a [u8],
    pos: usize,
    id: PageId,
}

impl Reader<'_> {
    fn bytes(&mut self, len: impl Into<usize>) -> Result<&[u8]> {
        let end = self.pos + len.into();
        if end > self.page.len() {
            return corrupt(format!("un nodo se sale de la pagina {}", self.id));
        }
        let bytes = &self.page[self.pos..end];
        self.pos = end;
        Ok(bytes)
    }

    fn u16(&mut self) -> Result<u16> {
        Ok(u16::from_le_bytes(
            self.bytes(2usize)?.try_into().expect("2 bytes"),
        ))
    }

    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_le_bytes(
            self.bytes(4usize)?.try_into().expect("4 bytes"),
        ))
    }
}

/// Cual de los hijos de un nodo interno puede tener la clave (0 = `first`).
fn child_index(entries: &[(Vec<u8>, PageId)], key: &[u8]) -> usize {
    entries.partition_point(|(k, _)| k.as_slice() <= key)
}

fn child_at(first: PageId, entries: &[(Vec<u8>, PageId)], index: usize) -> PageId {
    if index == 0 {
        first
    } else {
        entries[index - 1].1
    }
}

fn too_deep<T>(depth: usize) -> Result<T> {
    corrupt(format!(
        "un arbol tiene mas de {MAX_DEPTH} niveles (¿un ciclo?) en el nivel {depth}"
    ))
}

/// Inicializa una pagina como un arbol vacio.
pub fn init(pager: &mut Pager, id: PageId) -> Result<()> {
    Node::Leaf { cells: Vec::new() }.write(pager, id)
}

/// Crea un arbol vacio y devuelve su raiz.
pub fn create(pager: &mut Pager) -> Result<PageId> {
    let root = freelist::alloc(pager)?;
    init(pager, root)?;
    Ok(root)
}

pub fn get(pager: &mut Pager, root: PageId, key: &[u8]) -> Result<Option<Vec<u8>>> {
    let mut id = root;
    for _ in 0..MAX_DEPTH {
        match Node::read(pager, id)? {
            Node::Internal { first, entries } => {
                id = child_at(first, &entries, child_index(&entries, key));
            }
            Node::Leaf { cells } => {
                return Ok(cells
                    .binary_search_by(|(k, _)| k.as_slice().cmp(key))
                    .ok()
                    .map(|i| cells[i].1.clone()));
            }
        }
    }
    too_deep(MAX_DEPTH)
}

/// Guarda el valor en la clave, reemplazando el que hubiera.
pub fn put(pager: &mut Pager, root: PageId, key: &[u8], value: &[u8]) -> Result<()> {
    if key.len() > MAX_KEY {
        return Err(Error::Invalid(format!(
            "la clave ocupa {} bytes y el maximo es {MAX_KEY}",
            key.len()
        )));
    }
    if leaf_cell_size(key, value) > MAX_CELL {
        return Err(Error::Invalid(format!(
            "el valor ocupa {} bytes y el maximo es {}",
            value.len(),
            MAX_CELL - 4 - key.len()
        )));
    }

    if let Some((separator, right)) = put_in(pager, root, key, value, 0)? {
        // la raiz se partio: su mitad izquierda se muda a una pagina nueva
        let left = freelist::alloc(pager)?;
        let copy = *pager.read(root)?;
        *pager.write(left)? = copy;
        Node::Internal {
            first: left,
            entries: vec![(separator, right)],
        }
        .write(pager, root)?;
    }
    Ok(())
}

/// Si el nodo se tuvo que partir devuelve la clave que sube y la pagina nueva.
fn put_in(
    pager: &mut Pager,
    id: PageId,
    key: &[u8],
    value: &[u8],
    depth: usize,
) -> Result<Option<(Vec<u8>, PageId)>> {
    if depth > MAX_DEPTH {
        return too_deep(depth);
    }
    match Node::read(pager, id)? {
        Node::Leaf { mut cells } => {
            match cells.binary_search_by(|(k, _)| k.as_slice().cmp(key)) {
                Ok(i) => cells[i].1 = value.to_vec(),
                Err(i) => cells.insert(i, (key.to_vec(), value.to_vec())),
            }
            let node = Node::Leaf { cells };
            if node.size() <= PAGE_SIZE {
                node.write(pager, id)?;
                return Ok(None);
            }
            let Node::Leaf { mut cells } = node else {
                unreachable!()
            };
            let sizes: Vec<usize> = cells.iter().map(|(k, v)| leaf_cell_size(k, v)).collect();
            let right_cells = cells.split_off(split_point(&sizes, false)?);
            let separator = right_cells[0].0.clone();
            let right = freelist::alloc(pager)?;
            Node::Leaf { cells: right_cells }.write(pager, right)?;
            Node::Leaf { cells }.write(pager, id)?;
            Ok(Some((separator, right)))
        }
        Node::Internal { first, mut entries } => {
            let index = child_index(&entries, key);
            let child = child_at(first, &entries, index);
            let Some(split) = put_in(pager, child, key, value, depth + 1)? else {
                return Ok(None);
            };
            entries.insert(index, split);
            let node = Node::Internal { first, entries };
            if node.size() <= PAGE_SIZE {
                node.write(pager, id)?;
                return Ok(None);
            }
            let Node::Internal { first, mut entries } = node else {
                unreachable!()
            };
            let sizes: Vec<usize> = entries.iter().map(|(k, _)| entry_size(k)).collect();
            let mut right_entries = entries.split_off(split_point(&sizes, true)?);
            // la entrada del medio no queda en ninguno de los dos: sube al padre
            let (separator, right_first) = right_entries.remove(0);
            let right = freelist::alloc(pager)?;
            Node::Internal {
                first: right_first,
                entries: right_entries,
            }
            .write(pager, right)?;
            Node::Internal { first, entries }.write(pager, id)?;
            Ok(Some((separator, right)))
        }
    }
}

/// Donde partir un nodo que no entra para que las dos mitades queden parecidas.
/// En una hoja la mitad derecha empieza en el indice devuelto; en un nodo interno
/// esa entrada es la que sube al padre.
fn split_point(sizes: &[usize], internal: bool) -> Result<usize> {
    let total: usize = sizes.iter().sum();
    let fits = |bytes: usize| NODE_HEADER + bytes <= PAGE_SIZE;
    let mut best: Option<(usize, usize)> = None;
    let mut left = 0;
    for (i, size) in sizes.iter().enumerate() {
        let right = if internal {
            total - left - size
        } else {
            total - left
        };
        let valid = internal || i > 0;
        if valid && fits(left) && fits(right) {
            let diff = left.abs_diff(right);
            if best.is_none_or(|(_, best_diff)| diff < best_diff) {
                best = Some((i, diff));
            }
        }
        left += size;
    }
    match best {
        Some((i, _)) => Ok(i),
        None => corrupt("no se pudo partir un nodo"),
    }
}

/// Como quedo un nodo despues de borrar.
#[derive(PartialEq, Eq)]
enum Fill {
    Ok,
    /// Ocupa menos de MIN_FILL: el padre intenta juntarlo con un vecino.
    Low,
    Empty,
}

/// Por debajo de esto un nodo intenta juntarse con un vecino.
const MIN_FILL: usize = PAGE_SIZE / 4;

fn fill_of(node: &Node) -> Fill {
    match node {
        Node::Leaf { cells } if cells.is_empty() => Fill::Empty,
        Node::Internal { entries, .. } if entries.is_empty() => Fill::Low,
        _ if node.size() < MIN_FILL => Fill::Low,
        _ => Fill::Ok,
    }
}

/// Borra la clave y devuelve el valor que tenia.
pub fn delete(pager: &mut Pager, root: PageId, key: &[u8]) -> Result<Option<Vec<u8>>> {
    let (old, fill) = delete_in(pager, root, key, 0)?;
    if fill == Fill::Empty {
        // se vacio todo el arbol
        init(pager, root)?;
    }
    // si a la raiz le queda un solo hijo, el hijo pasa a ser la raiz (en su pagina)
    while let Node::Internal { first, entries } = Node::read(pager, root)? {
        if !entries.is_empty() {
            break;
        }
        let copy = *pager.read(first)?;
        *pager.write(root)? = copy;
        freelist::free(pager, first)?;
    }
    Ok(old)
}

/// Devuelve el valor borrado y como quedo el nodo.
fn delete_in(
    pager: &mut Pager,
    id: PageId,
    key: &[u8],
    depth: usize,
) -> Result<(Option<Vec<u8>>, Fill)> {
    if depth > MAX_DEPTH {
        return too_deep(depth);
    }
    match Node::read(pager, id)? {
        Node::Leaf { mut cells } => {
            let Ok(i) = cells.binary_search_by(|(k, _)| k.as_slice().cmp(key)) else {
                return Ok((None, Fill::Ok));
            };
            let (_, old) = cells.remove(i);
            let node = Node::Leaf { cells };
            node.write(pager, id)?;
            Ok((Some(old), fill_of(&node)))
        }
        Node::Internal { first, mut entries } => {
            let index = child_index(&entries, key);
            let child = child_at(first, &entries, index);
            let (old, fill) = delete_in(pager, child, key, depth + 1)?;
            match fill {
                Fill::Ok => return Ok((old, Fill::Ok)),
                Fill::Empty if entries.is_empty() => {
                    // era el unico hijo: este nodo tambien queda vacio
                    freelist::free(pager, child)?;
                    return Ok((old, Fill::Empty));
                }
                _ if entries.is_empty() => return Ok((old, Fill::Low)), // no tiene vecinos
                _ => merge_children(pager, first, &mut entries, index)?,
            }
            let node = Node::Internal { first, entries };
            node.write(pager, id)?;
            Ok((old, fill_of(&node)))
        }
    }
}

/// Junta el hijo numero `index` con un vecino si entre los dos entran en una pagina:
/// todo queda en el de la izquierda, el de la derecha se libera y su entrada sale
/// del padre. Un hijo vacio siempre se puede juntar.
fn merge_children(
    pager: &mut Pager,
    first: PageId,
    entries: &mut Vec<(Vec<u8>, PageId)>,
    index: usize,
) -> Result<()> {
    // `right` es la posicion del de la derecha de la pareja (siempre >= 1)
    let right = index.max(1);
    let left_id = child_at(first, entries, right - 1);
    let (separator, right_id) = entries[right - 1].clone();
    let merged = match (Node::read(pager, left_id)?, Node::read(pager, right_id)?) {
        (Node::Leaf { mut cells }, Node::Leaf { cells: more }) => {
            cells.extend(more);
            Node::Leaf { cells }
        }
        (
            Node::Internal {
                first: left_first,
                entries: mut left_entries,
            },
            Node::Internal {
                first: right_first,
                entries: right_entries,
            },
        ) => {
            // el separador baja del padre para apuntar al primer hijo de la derecha
            left_entries.push((separator, right_first));
            left_entries.extend(right_entries);
            Node::Internal {
                first: left_first,
                entries: left_entries,
            }
        }
        _ => return corrupt("dos nodos hermanos son de distinto tipo"),
    };
    if merged.size() <= PAGE_SIZE {
        merged.write(pager, left_id)?;
        freelist::free(pager, right_id)?;
        entries.remove(right - 1);
    }
    Ok(())
}

/// Recorre en orden las claves entre `start` y `end`.
pub fn range(
    pager: &mut Pager,
    root: PageId,
    start: Bound<&[u8]>,
    end: Bound<&[u8]>,
    mut visit: impl FnMut(&[u8], &[u8]) -> Result<()>,
) -> Result<()> {
    range_in(pager, root, start, end, &mut visit, 0)?;
    Ok(())
}

fn past_end(key: &[u8], end: Bound<&[u8]>) -> bool {
    match end {
        Bound::Included(e) => key > e,
        Bound::Excluded(e) => key >= e,
        Bound::Unbounded => false,
    }
}

/// Devuelve false cuando ya se paso el final y no hay que seguir.
fn range_in(
    pager: &mut Pager,
    id: PageId,
    start: Bound<&[u8]>,
    end: Bound<&[u8]>,
    visit: &mut impl FnMut(&[u8], &[u8]) -> Result<()>,
    depth: usize,
) -> Result<bool> {
    if depth > MAX_DEPTH {
        return too_deep(depth);
    }
    match Node::read(pager, id)? {
        Node::Leaf { cells } => {
            for (key, value) in &cells {
                let before_start = match start {
                    Bound::Included(s) => key.as_slice() < s,
                    Bound::Excluded(s) => key.as_slice() <= s,
                    Bound::Unbounded => false,
                };
                if before_start {
                    continue;
                }
                if past_end(key, end) {
                    return Ok(false);
                }
                visit(key, value)?;
            }
            Ok(true)
        }
        Node::Internal { first, entries } => {
            let from = match start {
                Bound::Included(s) | Bound::Excluded(s) => child_index(&entries, s),
                Bound::Unbounded => 0,
            };
            for index in from..=entries.len() {
                // todas las claves del hijo son >= su separador
                if index > 0 && past_end(&entries[index - 1].0, end) {
                    return Ok(false);
                }
                let child = child_at(first, &entries, index);
                if !range_in(pager, child, start, end, visit, depth + 1)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
    }
}

/// La clave mas grande del arbol.
pub fn last_key(pager: &mut Pager, root: PageId) -> Result<Option<Vec<u8>>> {
    // se baja siempre por el ultimo hijo; como las hojas vacias se sacan del
    // arbol, la ultima hoja solo puede estar vacia si el arbol esta vacio
    let mut id = root;
    for _ in 0..MAX_DEPTH {
        match Node::read(pager, id)? {
            Node::Internal { first, entries } => {
                id = entries.last().map_or(first, |(_, child)| *child);
            }
            Node::Leaf { cells } => return Ok(cells.last().map(|(key, _)| key.clone())),
        }
    }
    too_deep(MAX_DEPTH)
}

/// Todas las paginas del arbol.
fn pages(pager: &mut Pager, root: PageId) -> Result<Vec<PageId>> {
    let mut seen = HashSet::new();
    let mut pending = vec![root];
    while let Some(id) = pending.pop() {
        if !seen.insert(id) {
            return corrupt(format!("el arbol con raiz {root} tiene un ciclo"));
        }
        if let Node::Internal { first, entries } = Node::read(pager, id)? {
            pending.push(first);
            pending.extend(entries.iter().map(|(_, child)| *child));
        }
    }
    Ok(seen.into_iter().collect())
}

/// Deja el arbol vacio, liberando todas sus paginas menos la raiz.
pub fn clear(pager: &mut Pager, root: PageId) -> Result<()> {
    for id in pages(pager, root)? {
        if id != root {
            freelist::free(pager, id)?;
        }
    }
    init(pager, root)
}

/// Libera todas las paginas del arbol, incluida la raiz.
pub fn destroy(pager: &mut Pager, root: PageId) -> Result<()> {
    for id in pages(pager, root)? {
        freelist::free(pager, id)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::{Rng, TempFile};
    use std::collections::BTreeMap;

    /// Un archivo con la cabecera y un arbol vacio en la pagina 1.
    fn setup(tmp: &TempFile) -> (Pager, PageId) {
        let mut pager = Pager::open(tmp.path()).unwrap();
        pager.append().unwrap();
        let root = pager.append().unwrap();
        init(&mut pager, root).unwrap();
        (pager, root)
    }

    fn all(pager: &mut Pager, root: PageId) -> Vec<(Vec<u8>, Vec<u8>)> {
        let mut out = Vec::new();
        range(pager, root, Bound::Unbounded, Bound::Unbounded, |k, v| {
            out.push((k.to_vec(), v.to_vec()));
            Ok(())
        })
        .unwrap();
        out
    }

    fn depth(pager: &mut Pager, root: PageId) -> usize {
        let mut id = root;
        let mut depth = 1;
        while let Node::Internal { first, .. } = Node::read(pager, id).unwrap() {
            id = first;
            depth += 1;
        }
        depth
    }

    #[test]
    fn muchas_claves_en_orden_y_desorden() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        let mut rng = Rng::new(7);
        let mut keys: Vec<u32> = (0..20_000).collect();
        rng.shuffle(&mut keys);
        for &k in &keys {
            put(
                &mut pager,
                root,
                &k.to_be_bytes(),
                &k.to_le_bytes().repeat(25),
            )
            .unwrap();
        }
        // celdas de 108 bytes: unas 37 por hoja y cientos de hojas
        assert!(
            depth(&mut pager, root) >= 3,
            "tiene que haber varios niveles"
        );

        let stored = all(&mut pager, root);
        assert_eq!(stored.len(), 20_000);
        for (i, (k, v)) in stored.iter().enumerate() {
            assert_eq!(k, &(i as u32).to_be_bytes());
            assert_eq!(v, &(i as u32).to_le_bytes().repeat(25));
        }
        assert_eq!(
            get(&mut pager, root, &1234u32.to_be_bytes()).unwrap(),
            Some(1234u32.to_le_bytes().repeat(25))
        );
        assert_eq!(
            get(&mut pager, root, &99_999u32.to_be_bytes()).unwrap(),
            None
        );
        assert_eq!(
            last_key(&mut pager, root).unwrap(),
            Some(19_999u32.to_be_bytes().to_vec())
        );
    }

    /// Operaciones al azar comparadas contra el BTreeMap de la biblioteca estandar.
    #[test]
    fn se_comporta_igual_que_un_btreemap() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        let mut model: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
        let mut rng = Rng::new(42);

        for step in 0..30_000 {
            // claves cortas y largas, y valores de hasta el maximo, para forzar
            // particiones con celdas de tamaños muy distintos
            let key_len = if rng.below(10) == 0 {
                1 + rng.below(MAX_KEY)
            } else {
                1 + rng.below(6)
            };
            let key: Vec<u8> = (0..key_len).map(|_| b'a' + rng.below(4) as u8).collect();
            // mas borrados en la segunda mitad, para que el arbol se achique
            let deletes = if step < 15_000 { 3 } else { 6 };
            let op = rng.below(10);
            if op < 9 - deletes {
                let max_value = MAX_CELL - 4 - key.len();
                let len = if rng.below(20) == 0 {
                    max_value
                } else {
                    rng.below(100)
                };
                let value = vec![step as u8; len];
                put(&mut pager, root, &key, &value).unwrap();
                model.insert(key, value);
            } else if op < 9 {
                assert_eq!(delete(&mut pager, root, &key).unwrap(), model.remove(&key));
            } else {
                assert_eq!(
                    get(&mut pager, root, &key).unwrap(),
                    model.get(&key).cloned()
                );
            }
            if step % 5000 == 0 {
                pager.flush().unwrap();
            }
        }
        let expected: Vec<_> = model.clone().into_iter().collect();
        assert_eq!(all(&mut pager, root), expected);
        assert_eq!(
            last_key(&mut pager, root).unwrap(),
            model.keys().next_back().cloned()
        );

        // rangos
        type KeyRange<'a> = (Bound<&'a [u8]>, Bound<&'a [u8]>);
        let bounds: [KeyRange<'_>; 4] = [
            (Bound::Excluded(b"b"), Bound::Included(b"c")),
            (Bound::Included(b"ab"), Bound::Excluded(b"ad")),
            (Bound::Unbounded, Bound::Excluded(b"b")),
            (Bound::Included(b"cc"), Bound::Unbounded),
        ];
        for (from, to) in bounds {
            let mut got = Vec::new();
            range(&mut pager, root, from, to, |k, _| {
                got.push(k.to_vec());
                Ok(())
            })
            .unwrap();
            let want: Vec<_> = model
                .range::<[u8], _>((from, to))
                .map(|(k, _)| k.clone())
                .collect();
            assert_eq!(got, want);
        }
    }

    #[test]
    fn borrar_todo_libera_las_paginas() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        let mut keys: Vec<u32> = (0..5000).collect();
        for &k in &keys {
            put(&mut pager, root, &k.to_be_bytes(), &[0; 50]).unwrap();
        }
        let size = pager.page_count();
        assert!(pages(&mut pager, root).unwrap().len() > 50);

        Rng::new(1).shuffle(&mut keys);
        for &k in &keys {
            assert!(
                delete(&mut pager, root, &k.to_be_bytes())
                    .unwrap()
                    .is_some()
            );
        }
        assert_eq!(
            pages(&mut pager, root).unwrap(),
            [root],
            "solo queda la raiz"
        );
        assert!(all(&mut pager, root).is_empty());

        // al volver a llenarlo usa las paginas liberadas
        for &k in &keys {
            put(&mut pager, root, &k.to_be_bytes(), &[0; 50]).unwrap();
        }
        assert!(
            pager.page_count() <= size + 5,
            "{} > {size}",
            pager.page_count()
        );
    }

    #[test]
    fn borrar_salteado_junta_los_nodos() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        let mut keys: Vec<u32> = (0..20_000).collect();
        for &k in &keys {
            put(&mut pager, root, &k.to_be_bytes(), &[7; 40]).unwrap();
        }
        let before = pages(&mut pager, root).unwrap().len();

        // se borran 3 de cada 4 claves, al azar: ninguna hoja queda vacia del todo,
        // pero casi todas quedan con poco
        Rng::new(9).shuffle(&mut keys);
        let (gone, kept) = keys.split_at(15_000);
        for k in gone {
            delete(&mut pager, root, &k.to_be_bytes()).unwrap();
        }
        let after = pages(&mut pager, root).unwrap().len();
        assert!(
            after * 2 < before,
            "{before} paginas antes y {after} despues"
        );

        let mut kept = kept.to_vec();
        kept.sort();
        let stored: Vec<u32> = all(&mut pager, root)
            .iter()
            .map(|(k, _)| u32::from_be_bytes(k[..].try_into().unwrap()))
            .collect();
        assert_eq!(stored, kept);
    }

    #[test]
    fn borrar_el_final_y_seguir_agregando() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        for k in 0..3000u32 {
            put(&mut pager, root, &k.to_be_bytes(), &[1; 20]).unwrap();
        }
        let size = pager.page_count();
        for round in 0..5u32 {
            for k in 2000..3000u32 {
                delete(&mut pager, root, &(k + round * 1000).to_be_bytes()).unwrap();
            }
            assert_eq!(
                last_key(&mut pager, root).unwrap(),
                Some(1999u32.to_be_bytes().to_vec())
            );
            for k in 3000..4000u32 {
                put(
                    &mut pager,
                    root,
                    &(k + round * 1000).to_be_bytes(),
                    &[1; 20],
                )
                .unwrap();
            }
        }
        // las hojas que se vacian se reusan para las claves nuevas
        assert!(
            pager.page_count() <= size + 2,
            "{} > {size}",
            pager.page_count()
        );
        assert_eq!(all(&mut pager, root).len(), 3000);
    }

    #[test]
    fn clear_y_destroy_liberan_las_paginas() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        for k in 0..5000u32 {
            put(&mut pager, root, &k.to_be_bytes(), &[0; 50]).unwrap();
        }
        let size = pager.page_count();
        clear(&mut pager, root).unwrap();
        assert!(all(&mut pager, root).is_empty());

        for k in 0..5000u32 {
            put(&mut pager, root, &k.to_be_bytes(), &[0; 50]).unwrap();
        }
        assert_eq!(pager.page_count(), size);

        let other = create(&mut pager).unwrap();
        destroy(&mut pager, other).unwrap();
        assert_eq!(
            create(&mut pager).unwrap(),
            other,
            "reusa la pagina liberada"
        );
    }

    #[test]
    fn clave_o_valor_demasiado_grandes() {
        let tmp = TempFile::new();
        let (mut pager, root) = setup(&tmp);
        assert!(matches!(
            put(&mut pager, root, &[0; MAX_KEY + 1], b""),
            Err(Error::Invalid(_))
        ));
        assert!(matches!(
            put(&mut pager, root, b"k", &vec![0; MAX_CELL]),
            Err(Error::Invalid(_))
        ));
    }
}
