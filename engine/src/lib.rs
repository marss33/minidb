//! Motor de almacenamiento de minidb.
//!
//! El parser y el ejecutor de SQL siguen en Python; este crate solo se encarga
//! de guardar las tablas y los indices en un archivo. Esta dividido en capas,
//! de mas baja a mas alta:
//!
//! - [`pager`]: lee y escribe paginas de 4 KB, con una cache y un journal para
//!   que un corte de luz no deje el archivo a medias.
//! - [`freelist`]: las paginas que se liberan, para reusarlas.
//! - [`btree`]: un B+tree de claves y valores de bytes.
//! - [`record`] y [`keys`]: como se convierten las filas y los valores en bytes.
//! - [`storage`]: tablas, indices y el catalogo, cada uno en su B-tree.
//! - `python`: la interfaz que ve Python (solo con la feature `python`).

pub mod btree;
pub mod error;
pub mod freelist;
pub mod keys;
pub mod pager;
pub mod record;
pub mod storage;

#[cfg(feature = "python")]
mod python;
#[cfg(test)]
mod test_util;

pub use error::{Error, Result};
pub use record::{Row, Value};
pub use storage::Storage;
