CREATE TABLE alumnos (id INT PRIMARY KEY, nombre TEXT NOT NULL);
CREATE TABLE notas (alumno INT NOT NULL, materia TEXT NOT NULL, nota INT);
CREATE INDEX notas_alumno ON notas (alumno);

INSERT INTO alumnos VALUES (1, 'Ana'), (2, 'Bruno'), (3, 'Carla'), (4, 'Diego');
INSERT INTO notas VALUES
  (1, 'Algebra', 9), (1, 'Fisica', 7),
  (2, 'Algebra', 4), (2, 'Fisica', 6),
  (3, 'Algebra', 10);

-- el promedio de cada alumno, aunque no tenga notas (LEFT JOIN)
SELECT a.nombre, COUNT(n.nota) AS materias, AVG(n.nota) AS promedio
FROM alumnos a LEFT JOIN notas n ON n.alumno = a.id
GROUP BY a.nombre
ORDER BY promedio DESC;

-- como se busca: la PRIMARY KEY y el indice evitan recorrer las tablas
EXPLAIN SELECT a.nombre, n.nota FROM alumnos a JOIN notas n ON n.alumno = a.id WHERE a.id = 2;

-- una transaccion: o se guardan los dos cambios o ninguno
BEGIN;
UPDATE notas SET nota = nota + 1 WHERE alumno = 2;
DELETE FROM notas WHERE nota < 6;
COMMIT;

SELECT a.nombre, n.materia, n.nota FROM alumnos a JOIN notas n ON n.alumno = a.id ORDER BY a.nombre;
