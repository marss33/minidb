# minidb

Base de datos que estoy haciendo desde cero para entender cómo funcionan por dentro. El SQL se parsea y ejecuta en Python, y las tablas se guardan en disco con un motor en Rust (B-trees, índices y journal).

Se instala con `pip install -e .` (hace falta Rust) y se prueba con `python -m minidb --db datos.db`.
