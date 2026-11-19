//! Paginas libres.
//!
//! Las paginas que se liberan forman una lista enlazada: la cabecera (pagina 0)
//! guarda la primera en el byte 8, y cada pagina libre guarda la siguiente en sus
//! primeros 4 bytes (0 = no hay mas). Antes de agrandar el archivo se usa una de estas.

use crate::error::Result;
use crate::pager::{PageId, Pager};

const HEADER_PAGE: PageId = 0;
const HEAD_AT: usize = 8;

/// Devuelve una pagina llena de ceros: una libre si hay, o una nueva al final.
pub fn alloc(pager: &mut Pager) -> Result<PageId> {
    let free = read_u32(pager.read(HEADER_PAGE)?, HEAD_AT);
    if free == 0 {
        return pager.append();
    }
    let next = read_u32(pager.read(free)?, 0);
    write_u32(pager.write(HEADER_PAGE)?, HEAD_AT, next);
    pager.write(free)?.fill(0);
    Ok(free)
}

pub fn free(pager: &mut Pager, id: PageId) -> Result<()> {
    debug_assert!(id > 1, "la cabecera y el catalogo no se liberan nunca");
    let head = read_u32(pager.read(HEADER_PAGE)?, HEAD_AT);
    let page = pager.write(id)?;
    page.fill(0);
    write_u32(page, 0, head);
    write_u32(pager.write(HEADER_PAGE)?, HEAD_AT, id);
    Ok(())
}

pub(crate) fn read_u32(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(bytes[at..at + 4].try_into().expect("son 4 bytes"))
}

pub(crate) fn write_u32(bytes: &mut [u8], at: usize, value: u32) {
    bytes[at..at + 4].copy_from_slice(&value.to_le_bytes());
}
