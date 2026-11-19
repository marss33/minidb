//! El pager ve el archivo como un arreglo de paginas de tamaño fijo.
//!
//! Las paginas que se leen quedan en una cache. Las que se modifican se marcan
//! como sucias y recien se escriben al archivo en [`Pager::flush`] (el commit);
//! hasta ese momento los cambios solo existen en memoria y [`Pager::rollback`]
//! los descarta.
//!
//! # Journal
//!
//! Si se corta la luz mientras se escriben las paginas, el archivo quedaria con
//! algunas paginas nuevas y otras viejas. Para evitarlo, antes de tocar el archivo
//! se guarda una copia de las paginas originales en `<archivo>-journal`:
//!
//! 1. se escribe el journal y se espera a que llegue al disco
//! 2. se escriben las paginas nuevas en el archivo
//! 3. se borra el journal: en este momento el commit esta hecho
//!
//! Si al abrir hay un journal completo, el commit no termino: se vuelven a poner
//! las paginas originales. Si el journal esta incompleto, el corte fue en el paso 1
//! y el archivo todavia no se toco.
//!
//! ```text
//! journal = "MDBJRNL1" | paginas que tenia el archivo (u32) | cantidad (u32)
//!           | por cada pagina: numero (u32) | checksum (u32) | contenido (4096 bytes)
//! ```

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::error::{Result, corrupt};

pub const PAGE_SIZE: usize = 4096;

/// Cuantas paginas limpias se guardan en memoria como maximo (1 MB).
/// Las sucias no se pueden sacar de la cache hasta el proximo flush.
const CACHE_PAGES: usize = 256;

const JOURNAL_MAGIC: &[u8; 8] = b"MDBJRNL1";
const JOURNAL_HEADER: usize = 16;
const JOURNAL_ENTRY: usize = 8 + PAGE_SIZE;

pub type PageId = u32;
pub type Page = [u8; PAGE_SIZE];

pub struct Pager {
    file: File,
    journal: PathBuf,
    /// Paginas que tiene el archivo, contando las nuevas que todavia no se escribieron.
    page_count: u32,
    /// Paginas que tenia el archivo en el ultimo commit.
    committed_count: u32,
    cache: HashMap<PageId, Box<Page>>,
    dirty: BTreeSet<PageId>,
    /// Como estaban en el ultimo commit las paginas que se modificaron desde entonces.
    originals: BTreeMap<PageId, Box<Page>>,
    /// Para los tests: simula un corte despues de escribir esta cantidad de paginas.
    #[cfg(test)]
    pub(crate) crash_after: Option<usize>,
}

impl Pager {
    pub fn open(path: &Path) -> Result<Pager> {
        recover(path)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        let len = file.metadata()?.len();
        if len % PAGE_SIZE as u64 != 0 {
            return corrupt(format!(
                "el tamaño ({len} bytes) no es multiplo de {PAGE_SIZE}"
            ));
        }
        let Ok(page_count) = u32::try_from(len / PAGE_SIZE as u64) else {
            return corrupt("tiene demasiadas paginas");
        };
        Ok(Pager {
            file,
            journal: journal_path(path),
            page_count,
            committed_count: page_count,
            cache: HashMap::new(),
            dirty: BTreeSet::new(),
            originals: BTreeMap::new(),
            #[cfg(test)]
            crash_after: None,
        })
    }

    pub fn page_count(&self) -> u32 {
        self.page_count
    }

    pub fn read(&mut self, id: PageId) -> Result<&Page> {
        self.load(id)?;
        Ok(&self.cache[&id])
    }

    /// Devuelve la pagina para modificarla y la marca como sucia.
    pub fn write(&mut self, id: PageId) -> Result<&mut Page> {
        self.load(id)?;
        if id < self.committed_count && !self.originals.contains_key(&id) {
            // es la primera vez que se toca desde el commit: lo que hay en la
            // cache es todavia lo mismo que en el archivo
            self.originals.insert(id, self.cache[&id].clone());
        }
        self.dirty.insert(id);
        Ok(self.cache.get_mut(&id).expect("load la deja en la cache"))
    }

    /// Agrega una pagina vacia al final del archivo.
    pub fn append(&mut self) -> Result<PageId> {
        let id = self.page_count;
        let Some(next) = id.checked_add(1) else {
            return corrupt("no entran mas paginas en el archivo");
        };
        self.make_room();
        self.cache.insert(id, Box::new([0; PAGE_SIZE]));
        self.dirty.insert(id);
        self.page_count = next;
        Ok(id)
    }

    /// Guarda en el archivo todos los cambios desde el ultimo commit.
    pub fn flush(&mut self) -> Result<()> {
        if self.dirty.is_empty() {
            return Ok(());
        }

        // 1. el journal con las paginas originales
        let mut journal = Vec::with_capacity(JOURNAL_HEADER + self.originals.len() * JOURNAL_ENTRY);
        journal.extend_from_slice(JOURNAL_MAGIC);
        journal.extend_from_slice(&self.committed_count.to_le_bytes());
        journal.extend_from_slice(&(self.originals.len() as u32).to_le_bytes());
        for (&id, page) in &self.originals {
            journal.extend_from_slice(&id.to_le_bytes());
            journal.extend_from_slice(&checksum(id, &page[..]).to_le_bytes());
            journal.extend_from_slice(&page[..]);
        }
        let mut file = File::create(&self.journal)?;
        file.write_all(&journal)?;
        file.sync_all()?;
        drop(file);

        // 2. las paginas nuevas
        for (written, &id) in self.dirty.iter().enumerate() {
            if self.simulated_crash(written) {
                return Err(io::Error::other("corte de luz simulado").into());
            }
            self.file.seek(SeekFrom::Start(offset(id)))?;
            self.file.write_all(&self.cache[&id][..])?;
        }
        self.file.sync_all()?;

        // 3. listo
        fs::remove_file(&self.journal)?;
        self.committed_count = self.page_count;
        self.dirty.clear();
        self.originals.clear();
        Ok(())
    }

    #[cfg(test)]
    fn simulated_crash(&self, written: usize) -> bool {
        self.crash_after == Some(written)
    }

    #[cfg(not(test))]
    fn simulated_crash(&self, _written: usize) -> bool {
        false
    }

    /// Descarta los cambios hechos desde el ultimo commit.
    pub fn rollback(&mut self) {
        for (id, page) in std::mem::take(&mut self.originals) {
            self.cache.insert(id, page);
        }
        for &id in &self.dirty {
            if id >= self.committed_count {
                self.cache.remove(&id);
            }
        }
        self.dirty.clear();
        self.page_count = self.committed_count;
    }

    fn load(&mut self, id: PageId) -> Result<()> {
        if self.cache.contains_key(&id) {
            return Ok(());
        }
        if id >= self.page_count {
            return corrupt(format!("se pidio la pagina {id} y hay {}", self.page_count));
        }
        self.make_room();
        let mut page = Box::new([0; PAGE_SIZE]);
        self.file.seek(SeekFrom::Start(offset(id)))?;
        self.file.read_exact(&mut page[..])?;
        self.cache.insert(id, page);
        Ok(())
    }

    /// Si la cache esta llena saca paginas limpias (cualquiera, todavia no hay LRU).
    fn make_room(&mut self) {
        let excess = (self.cache.len() + 1).saturating_sub(CACHE_PAGES);
        if excess == 0 {
            return;
        }
        let victims: Vec<PageId> = self
            .cache
            .keys()
            .filter(|id| !self.dirty.contains(id))
            .take(excess)
            .copied()
            .collect();
        for id in victims {
            self.cache.remove(&id);
        }
    }
}

pub fn journal_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push("-journal");
    PathBuf::from(name)
}

/// Si quedo un journal de un commit que no termino, deja el archivo como estaba antes.
pub fn recover(path: &Path) -> Result<()> {
    let journal = journal_path(path);
    let bytes = match fs::read(&journal) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    if let Some((count, pages)) = parse_journal(&bytes) {
        let mut file = OpenOptions::new().write(true).open(path)?;
        for (id, page) in pages {
            file.seek(SeekFrom::Start(offset(id)))?;
            file.write_all(page)?;
        }
        file.set_len(u64::from(count) * PAGE_SIZE as u64)?;
        file.sync_all()?;
    }
    // si estaba incompleto el corte fue antes de tocar el archivo: no hay nada que deshacer
    fs::remove_file(&journal)?;
    Ok(())
}

/// Las paginas guardadas en un journal: (numero, contenido).
type JournalPages<'a> = Vec<(PageId, &'a [u8])>;

fn parse_journal(bytes: &[u8]) -> Option<(u32, JournalPages<'_>)> {
    if bytes.len() < JOURNAL_HEADER || &bytes[..8] != JOURNAL_MAGIC {
        return None;
    }
    let count = u32::from_le_bytes(bytes[8..12].try_into().ok()?);
    let n = u32::from_le_bytes(bytes[12..16].try_into().ok()?) as usize;
    if bytes.len() != JOURNAL_HEADER + n * JOURNAL_ENTRY {
        return None;
    }
    let mut pages = Vec::with_capacity(n);
    for entry in bytes[JOURNAL_HEADER..].chunks_exact(JOURNAL_ENTRY) {
        let id = u32::from_le_bytes(entry[0..4].try_into().ok()?);
        let sum = u32::from_le_bytes(entry[4..8].try_into().ok()?);
        let page = &entry[8..];
        if checksum(id, page) != sum {
            return None;
        }
        pages.push((id, page));
    }
    Some((count, pages))
}

/// FNV-1a: no es criptografico, solo sirve para detectar paginas a medio escribir.
fn checksum(id: PageId, page: &[u8]) -> u32 {
    let mut hash: u32 = 0x811c_9dc5;
    for &byte in id.to_le_bytes().iter().chain(page) {
        hash ^= u32::from(byte);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    hash
}

fn offset(id: PageId) -> u64 {
    u64::from(id) * PAGE_SIZE as u64
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::TempFile;

    fn fill(pager: &mut Pager, id: PageId, byte: u8) {
        pager.write(id).unwrap().fill(byte);
    }

    #[test]
    fn lo_escrito_sobrevive_a_reabrir() {
        let tmp = TempFile::new();
        {
            let mut pager = Pager::open(tmp.path()).unwrap();
            let a = pager.append().unwrap();
            let b = pager.append().unwrap();
            pager.write(a).unwrap()[0] = 7;
            pager.write(b).unwrap()[PAGE_SIZE - 1] = 9;
            pager.flush().unwrap();
        }
        let mut pager = Pager::open(tmp.path()).unwrap();
        assert_eq!(pager.page_count(), 2);
        assert_eq!(pager.read(0).unwrap()[0], 7);
        assert_eq!(pager.read(1).unwrap()[PAGE_SIZE - 1], 9);
        assert!(!journal_path(tmp.path()).exists());
    }

    #[test]
    fn sin_flush_no_se_guarda_nada() {
        let tmp = TempFile::new();
        {
            let mut pager = Pager::open(tmp.path()).unwrap();
            pager.append().unwrap();
        }
        assert_eq!(Pager::open(tmp.path()).unwrap().page_count(), 0);
    }

    #[test]
    fn rollback_deja_todo_como_en_el_commit() {
        let tmp = TempFile::new();
        let mut pager = Pager::open(tmp.path()).unwrap();
        let a = pager.append().unwrap();
        fill(&mut pager, a, 1);
        pager.flush().unwrap();

        fill(&mut pager, a, 2);
        let b = pager.append().unwrap();
        fill(&mut pager, b, 3);
        pager.rollback();

        assert_eq!(pager.page_count(), 1);
        assert_eq!(pager.read(a).unwrap()[100], 1);
        assert!(pager.read(b).is_err());
        // y se puede seguir usando normalmente
        fill(&mut pager, a, 4);
        pager.flush().unwrap();
        drop(pager);
        assert_eq!(Pager::open(tmp.path()).unwrap().read(a).unwrap()[0], 4);
    }

    #[test]
    fn corte_a_mitad_del_commit_vuelve_al_commit_anterior() {
        let tmp = TempFile::new();
        {
            let mut pager = Pager::open(tmp.path()).unwrap();
            for byte in 0..4 {
                let id = pager.append().unwrap();
                fill(&mut pager, id, byte);
            }
            pager.flush().unwrap();

            // se modifican tres paginas, se agregan dos y se corta la luz
            // despues de escribir dos de las cinco
            for id in 1..4 {
                fill(&mut pager, id, 100);
            }
            for _ in 0..2 {
                let id = pager.append().unwrap();
                fill(&mut pager, id, 100);
            }
            pager.crash_after = Some(2);
            assert!(pager.flush().is_err());
        }
        assert!(journal_path(tmp.path()).exists());
        // el archivo quedo mezclado
        assert_eq!(fs::read(tmp.path()).unwrap()[PAGE_SIZE], 100);

        let mut pager = Pager::open(tmp.path()).unwrap();
        assert!(!journal_path(tmp.path()).exists());
        assert_eq!(pager.page_count(), 4);
        for id in 0..4 {
            assert!(pager.read(id).unwrap().iter().all(|&b| b == id as u8));
        }
    }

    #[test]
    fn journal_incompleto_se_ignora() {
        let tmp = TempFile::new();
        {
            let mut pager = Pager::open(tmp.path()).unwrap();
            let id = pager.append().unwrap();
            fill(&mut pager, id, 5);
            pager.flush().unwrap();
        }
        // un journal cortado a la mitad (el corte fue mientras se escribia)
        let mut journal = JOURNAL_MAGIC.to_vec();
        journal.extend_from_slice(&[1, 0, 0, 0, 1, 0, 0, 0, 0, 0]);
        fs::write(journal_path(tmp.path()), journal).unwrap();

        let mut pager = Pager::open(tmp.path()).unwrap();
        assert!(!journal_path(tmp.path()).exists());
        assert_eq!(pager.read(0).unwrap()[0], 5);
    }

    #[test]
    fn la_cache_no_crece_sin_limite() {
        let tmp = TempFile::new();
        let mut pager = Pager::open(tmp.path()).unwrap();
        let total = CACHE_PAGES as u32 * 3;
        for i in 0..total {
            let id = pager.append().unwrap();
            pager.write(id).unwrap()[0] = (i % 251) as u8;
            if i % 50 == 0 {
                pager.flush().unwrap();
            }
        }
        pager.flush().unwrap();
        for i in 0..total {
            assert_eq!(pager.read(i).unwrap()[0], (i % 251) as u8);
        }
        assert!(pager.cache.len() <= CACHE_PAGES);
    }

    #[test]
    fn pagina_fuera_de_rango() {
        let tmp = TempFile::new();
        let mut pager = Pager::open(tmp.path()).unwrap();
        assert!(pager.read(0).is_err());
    }
}
