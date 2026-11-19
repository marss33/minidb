//! Claves de los B-trees.
//!
//! El B-tree compara las claves byte a byte, asi que hay que codificar los valores
//! de forma que el orden de los bytes sea el mismo que el de los valores:
//!
//! - rowid: i64 en big endian con el bit de signo invertido, asi los negativos
//!   quedan antes que los positivos.
//! - NULL: `00`
//! - INT:  `01` + el entero igual que el rowid
//! - TEXT: `02` + el texto en utf-8 (cada byte `00` se escribe `00 FF`) + `00 00`
//!
//! Ningun valor codificado es prefijo de otro (por eso se escapan los `00` del
//! texto). Eso permite armar la clave de un indice pegando el valor y el rowid,
//! y buscar todas las filas con un valor dado como un rango de claves.

use crate::error::{Result, corrupt};
use crate::record::Value;

const NULL: u8 = 0;
const INT: u8 = 1;
const TEXT: u8 = 2;

/// La clave mas chica que no es NULL: los rangos de los indices empiezan aca,
/// porque en SQL ninguna comparacion con NULL es verdadera.
pub const FIRST_NOT_NULL: &[u8] = &[INT];

/// Pegado despues de un valor da una clave mas grande que todas las de ese valor
/// (que son el valor + 8 bytes de rowid) y mas chica que las del valor siguiente.
pub const AFTER_VALUE: [u8; 9] = [0xFF; 9];

pub fn rowid(rowid: i64) -> [u8; 8] {
    ((rowid as u64) ^ (1 << 63)).to_be_bytes()
}

pub fn decode_rowid(bytes: &[u8]) -> Result<i64> {
    match <[u8; 8]>::try_from(bytes) {
        Ok(bytes) => Ok((u64::from_be_bytes(bytes) ^ (1 << 63)) as i64),
        Err(_) => corrupt("una clave de tabla no mide 8 bytes"),
    }
}

pub fn value(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    match value {
        Value::Null => out.push(NULL),
        Value::Int(n) => {
            out.push(INT);
            out.extend_from_slice(&rowid(*n));
        }
        Value::Text(s) => {
            out.push(TEXT);
            for &byte in s.as_bytes() {
                out.push(byte);
                if byte == 0 {
                    out.push(0xFF);
                }
            }
            out.extend_from_slice(&[0, 0]);
        }
    }
    out
}

/// Clave de un indice: el valor de la columna y el rowid de la fila.
pub fn index_entry(column: &Value, row: i64) -> Vec<u8> {
    let mut key = value(column);
    key.extend_from_slice(&rowid(row));
    key
}

pub fn rowid_of_index_entry(key: &[u8]) -> Result<i64> {
    match key.len().checked_sub(8) {
        Some(start) => decode_rowid(&key[start..]),
        None => corrupt("una clave de indice es demasiado corta"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn los_rowids_quedan_en_orden() {
        let ids = [i64::MIN, -1000, -1, 0, 1, 255, 256, 1 << 40, i64::MAX];
        for pair in ids.windows(2) {
            assert!(rowid(pair[0]) < rowid(pair[1]), "{pair:?}");
        }
        for id in ids {
            assert_eq!(decode_rowid(&rowid(id)).unwrap(), id);
        }
    }

    #[test]
    fn los_valores_quedan_en_orden() {
        let text = |s: &str| Value::Text(s.into());
        // ya ordenados como los ordena SQL (y Python)
        let values = [
            Value::Null,
            Value::Int(i64::MIN),
            Value::Int(-5),
            Value::Int(0),
            Value::Int(7),
            Value::Int(i64::MAX),
            text(""),
            text("\0"),
            text("\0\0"),
            text("\0a"),
            text("a"),
            text("a\0"),
            text("a\0b"),
            text("ab"),
            text("b"),
            text("ñ"),
            text("🦀"),
        ];
        for pair in values.windows(2) {
            assert!(value(&pair[0]) < value(&pair[1]), "{pair:?}");
        }
    }

    #[test]
    fn ningun_valor_es_prefijo_de_otro() {
        let values = [
            Value::Null,
            Value::Int(1),
            Value::Text("a".into()),
            Value::Text("a\0".into()),
            Value::Text("ab".into()),
        ];
        for a in &values {
            for b in &values {
                if a != b {
                    assert!(!value(b).starts_with(&value(a)), "{a:?} {b:?}");
                }
            }
        }
    }

    #[test]
    fn claves_de_indice() {
        let a = index_entry(&Value::Int(5), 900);
        let b = index_entry(&Value::Int(5), 901);
        let c = index_entry(&Value::Int(6), -3);
        assert!(a < b && b < c);
        let mut after = value(&Value::Int(5));
        after.extend_from_slice(&AFTER_VALUE);
        assert!(b < after && after < c);
        assert_eq!(rowid_of_index_entry(&c).unwrap(), -3);
    }
}
