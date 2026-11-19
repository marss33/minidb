//! Formato de una fila en bytes.
//!
//! ```text
//! fila  = cantidad de valores (u16) + cada valor
//! valor = tag (u8) + datos
//!   0 NULL   sin datos
//!   1 INT    i64 (8 bytes)
//!   2 TEXT   largo en bytes (u32) + texto en utf-8
//! ```
//!
//! Todos los numeros van en little endian. La fila no guarda su propio largo:
//! se sabe donde termina leyendola.

use crate::error::{Error, Result, corrupt};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Null,
    Int(i64),
    Text(String),
}

pub type Row = Vec<Value>;

const TAG_NULL: u8 = 0;
const TAG_INT: u8 = 1;
const TAG_TEXT: u8 = 2;

pub fn encode(row: &[Value]) -> Result<Vec<u8>> {
    let count = u16::try_from(row.len())
        .map_err(|_| Error::Invalid(format!("una fila no puede tener {} valores", row.len())))?;
    let mut out = Vec::with_capacity(encoded_len(row));
    out.extend_from_slice(&count.to_le_bytes());
    for value in row {
        match value {
            Value::Null => out.push(TAG_NULL),
            Value::Int(n) => {
                out.push(TAG_INT);
                out.extend_from_slice(&n.to_le_bytes());
            }
            Value::Text(s) => {
                let len = u32::try_from(s.len())
                    .map_err(|_| Error::Invalid("el texto es demasiado largo".into()))?;
                out.push(TAG_TEXT);
                out.extend_from_slice(&len.to_le_bytes());
                out.extend_from_slice(s.as_bytes());
            }
        }
    }
    Ok(out)
}

fn encoded_len(row: &[Value]) -> usize {
    2 + row
        .iter()
        .map(|v| match v {
            Value::Null => 1,
            Value::Int(_) => 9,
            Value::Text(s) => 5 + s.len(),
        })
        .sum::<usize>()
}

/// Lee una fila que empieza en `buf[*pos]` y deja `pos` justo despues.
pub fn decode(buf: &[u8], pos: &mut usize) -> Result<Row> {
    let count = u16::from_le_bytes(take(buf, pos)?);
    let mut row = Vec::with_capacity(count.into());
    for _ in 0..count {
        let [tag] = take(buf, pos)?;
        row.push(match tag {
            TAG_NULL => Value::Null,
            TAG_INT => Value::Int(i64::from_le_bytes(take(buf, pos)?)),
            TAG_TEXT => {
                let len = u32::from_le_bytes(take(buf, pos)?) as usize;
                let bytes = take_slice(buf, pos, len)?;
                match std::str::from_utf8(bytes) {
                    Ok(s) => Value::Text(s.to_owned()),
                    Err(_) => return corrupt("un texto no es utf-8 valido"),
                }
            }
            other => return corrupt(format!("tipo de valor desconocido ({other})")),
        });
    }
    Ok(row)
}

fn take<const N: usize>(buf: &[u8], pos: &mut usize) -> Result<[u8; N]> {
    let slice = take_slice(buf, pos, N)?;
    Ok(slice
        .try_into()
        .expect("take_slice devuelve exactamente N bytes"))
}

fn take_slice<'a>(buf: &'a [u8], pos: &mut usize, len: usize) -> Result<&'a [u8]> {
    let end = pos.checked_add(len).filter(|&end| end <= buf.len());
    match end {
        Some(end) => {
            let slice = &buf[*pos..end];
            *pos = end;
            Ok(slice)
        }
        None => corrupt("una fila se sale de su pagina"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ida_y_vuelta() {
        let row = vec![
            Value::Int(-42),
            Value::Null,
            Value::Text("ñandú 🦀".into()),
            Value::Int(i64::MAX),
            Value::Text(String::new()),
        ];
        let bytes = encode(&row).unwrap();
        assert_eq!(bytes.len(), encoded_len(&row));

        let mut pos = 0;
        assert_eq!(decode(&bytes, &mut pos).unwrap(), row);
        assert_eq!(pos, bytes.len());
    }

    #[test]
    fn varias_filas_seguidas() {
        let a = vec![Value::Int(1)];
        let b = vec![Value::Text("dos".into()), Value::Null];
        let mut bytes = encode(&a).unwrap();
        bytes.extend(encode(&b).unwrap());

        let mut pos = 0;
        assert_eq!(decode(&bytes, &mut pos).unwrap(), a);
        assert_eq!(decode(&bytes, &mut pos).unwrap(), b);
    }

    #[test]
    fn bytes_cortados_dan_error_y_no_panic() {
        let bytes = encode(&[Value::Text("hola".into())]).unwrap();
        for len in 0..bytes.len() {
            let mut pos = 0;
            assert!(matches!(
                decode(&bytes[..len], &mut pos),
                Err(Error::Corrupt(_))
            ));
        }
    }

    #[test]
    fn tag_desconocido() {
        let mut pos = 0;
        assert!(matches!(
            decode(&[1, 0, 9], &mut pos),
            Err(Error::Corrupt(_))
        ));
    }
}
