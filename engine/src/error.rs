use std::fmt;
use std::io;

#[derive(Debug)]
pub enum Error {
    /// Fallo leer o escribir el archivo.
    Io(io::Error),
    /// El archivo existe pero no es una base de minidb.
    NotADatabase,
    /// El archivo es de minidb pero tiene datos que no tienen sentido.
    Corrupt(String),
    /// Lo que se pidio no se puede hacer (tabla repetida, fila muy grande, etc).
    Invalid(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "error de entrada/salida: {e}"),
            Error::NotADatabase => write!(f, "el archivo no es una base de datos de minidb"),
            Error::Corrupt(msg) => write!(f, "el archivo esta corrupto: {msg}"),
            Error::Invalid(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}

pub(crate) fn corrupt<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error::Corrupt(msg.into()))
}
