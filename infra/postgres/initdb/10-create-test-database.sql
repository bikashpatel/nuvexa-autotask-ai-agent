-- Executed once by the postgres image on first initialisation of the compose volume
-- (files in /docker-entrypoint-initdb.d run as POSTGRES_USER against POSTGRES_DB).
-- Creates the database used by `TEST_DATABASE_URL=...localhost:5432/nuvexa_test uv run pytest`.
-- Existing volumes are not re-initialised: run
--   docker compose exec postgres createdb -U nuvexa nuvexa_test
-- instead (or `docker compose down -v` to start from scratch).
CREATE DATABASE nuvexa_test;
