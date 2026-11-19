//! Lo que ve Python: el modulo `minidb._engine`.

use std::path::PathBuf;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyInt, PyList, PyString, PyTuple};

use crate::error::Error;
use crate::pager::PAGE_SIZE;
use crate::record::{Row, Value};
use crate::storage::{Limit, MAX_ROW, Storage};

create_exception!(
    _engine,
    StorageError,
    PyException,
    "Error del motor de almacenamiento."
);

impl From<Error> for PyErr {
    fn from(e: Error) -> PyErr {
        match e {
            // los errores del sistema llegan como OSError (FileNotFoundError, etc)
            Error::Io(e) => e.into(),
            other => StorageError::new_err(other.to_string()),
        }
    }
}

type PyRows<'py> = Vec<Vec<Bound<'py, PyAny>>>;

/// Una base de datos abierta en un archivo.
#[pyclass(module = "minidb._engine")]
struct Engine {
    storage: Option<Storage>,
}

impl Engine {
    fn storage(&mut self) -> PyResult<&mut Storage> {
        self.storage
            .as_mut()
            .ok_or_else(|| StorageError::new_err("la base de datos esta cerrada"))
    }
}

#[pymethods]
impl Engine {
    #[new]
    fn new(path: PathBuf) -> PyResult<Self> {
        Ok(Engine {
            storage: Some(Storage::open(&path)?),
        })
    }

    /// (tipo, nombre, tabla, sql) de cada tabla e indice.
    fn catalog(&mut self) -> PyResult<Vec<(&'static str, String, String, String)>> {
        Ok(self
            .storage()?
            .catalog()
            .into_iter()
            .map(|e| (e.kind.as_str(), e.name, e.table, e.sql))
            .collect())
    }

    fn create_table(&mut self, name: &str, sql: &str) -> PyResult<()> {
        Ok(self.storage()?.create_table(name, sql)?)
    }

    fn create_index(&mut self, name: &str, table: &str, column: usize, sql: &str) -> PyResult<()> {
        Ok(self.storage()?.create_index(name, table, column, sql)?)
    }

    fn drop_table(&mut self, name: &str) -> PyResult<()> {
        Ok(self.storage()?.drop_table(name)?)
    }

    fn drop_index(&mut self, name: &str) -> PyResult<()> {
        Ok(self.storage()?.drop_index(name)?)
    }

    fn insert(&mut self, table: &str, rows: PyRows<'_>) -> PyResult<Vec<i64>> {
        let rows: Vec<Row> = rows
            .iter()
            .map(|row| to_row(row))
            .collect::<PyResult<_>>()?;
        Ok(self.storage()?.insert(table, &rows)?)
    }

    fn scan<'py>(&mut self, py: Python<'py>, table: &str) -> PyResult<Bound<'py, PyList>> {
        let rows = self.storage()?.scan(table)?;
        to_python_rows(py, rows)
    }

    #[pyo3(signature = (index, low, high))]
    fn search<'py>(
        &mut self,
        py: Python<'py>,
        index: &str,
        low: Option<(Bound<'py, PyAny>, bool)>,
        high: Option<(Bound<'py, PyAny>, bool)>,
    ) -> PyResult<Bound<'py, PyList>> {
        let low = to_limit(low)?;
        let high = to_limit(high)?;
        let rows = self.storage()?.search(index, &low, &high)?;
        to_python_rows(py, rows)
    }

    fn update(&mut self, table: &str, changes: Vec<(i64, Vec<Bound<'_, PyAny>>)>) -> PyResult<()> {
        let changes: Vec<(i64, Row)> = changes
            .iter()
            .map(|(rowid, row)| Ok((*rowid, to_row(row)?)))
            .collect::<PyResult<_>>()?;
        Ok(self.storage()?.update(table, &changes)?)
    }

    fn delete(&mut self, table: &str, rowids: Vec<i64>) -> PyResult<()> {
        Ok(self.storage()?.delete(table, &rowids)?)
    }

    fn clear(&mut self, table: &str) -> PyResult<()> {
        Ok(self.storage()?.clear(table)?)
    }

    fn commit(&mut self) -> PyResult<()> {
        Ok(self.storage()?.commit()?)
    }

    fn rollback(&mut self) -> PyResult<()> {
        Ok(self.storage()?.rollback()?)
    }

    fn page_count(&mut self) -> PyResult<u32> {
        Ok(self.storage()?.page_count())
    }

    /// Cierra el archivo. Lo que no se commiteo se pierde.
    fn close(&mut self) {
        self.storage = None;
    }
}

fn to_row(row: &[Bound<'_, PyAny>]) -> PyResult<Row> {
    row.iter().map(to_value).collect()
}

fn to_limit(limit: Option<(Bound<'_, PyAny>, bool)>) -> PyResult<Limit> {
    limit
        .map(|(value, inclusive)| Ok((to_value(&value)?, inclusive)))
        .transpose()
}

fn to_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    // bool es subclase de int en Python, asi que hay que mirarlo antes
    if obj.is_none() {
        Ok(Value::Null)
    } else if obj.is_instance_of::<PyBool>() {
        Err(PyTypeError::new_err(
            "no se pueden guardar valores booleanos",
        ))
    } else if obj.is_instance_of::<PyInt>() {
        Ok(Value::Int(obj.extract()?))
    } else if obj.is_instance_of::<PyString>() {
        Ok(Value::Text(obj.extract()?))
    } else {
        let kind = obj.get_type().name()?;
        Err(PyTypeError::new_err(format!(
            "no se puede guardar un valor de tipo {kind}"
        )))
    }
}

fn to_python<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    Ok(match value {
        Value::Null => py.None().into_bound(py),
        Value::Int(n) => n.into_pyobject(py)?.into_any(),
        Value::Text(s) => s.into_pyobject(py)?.into_any(),
    })
}

/// Una lista de (rowid, fila), con cada fila como tupla.
fn to_python_rows(py: Python<'_>, rows: Vec<(i64, Row)>) -> PyResult<Bound<'_, PyList>> {
    let items = rows
        .iter()
        .map(|(rowid, row)| {
            let values = row
                .iter()
                .map(|v| to_python(py, v))
                .collect::<PyResult<Vec<_>>>()?;
            let row = PyTuple::new(py, values)?;
            PyTuple::new(py, [rowid.into_pyobject(py)?.into_any(), row.into_any()])
        })
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, items)
}

#[pymodule]
#[pyo3(name = "_engine")]
fn engine_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Engine>()?;
    m.add("StorageError", m.py().get_type::<StorageError>())?;
    m.add("PAGE_SIZE", PAGE_SIZE)?;
    m.add("MAX_ROW", MAX_ROW)?;
    Ok(())
}
