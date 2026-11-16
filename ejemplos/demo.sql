CREATE TABLE alumnos (id INT, nombre TEXT, nota INT);

INSERT INTO alumnos VALUES
  (1, 'Ana', 9),
  (2, 'Bruno', 6),
  (3, 'Carla', NULL),
  (4, 'Diego', 4),
  (5, 'Eva', 6);

SELECT * FROM alumnos;

-- los que aprobaron, de mejor a peor nota
SELECT nombre, nota FROM alumnos WHERE nota >= 6 ORDER BY nota DESC, nombre;

UPDATE alumnos SET nota = nota + 1 WHERE nota < 6;
DELETE FROM alumnos WHERE nota IS NULL;

SELECT * FROM alumnos ORDER BY nombre;
