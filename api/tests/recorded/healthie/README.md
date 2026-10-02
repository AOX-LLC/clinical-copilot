# Healthie fixtures

Everything here is **hand-built from Healthie's public API reference. None of it is a recording**,
and no Healthie account was ever used. The records are synthetic: invented names, invented
ids, invented dates.

- `world.json`: the records the fixture server answers with. Each field it holds is a field the
  adapter's queries select; values are made up.
- `schema/healthie-2025-11-30.graphql`: an excerpt of Healthie's reference, used to check that
  every query the adapter sends is valid. `schema/build_excerpt.py` rebuilds it (needs network;
  `--check` compares without writing).
- `../healthie_server.py`: executes the adapter's queries against `world.json` through the
  excerpt schema, so a query that is not valid against the excerpt fails the test that sent it.

Passing here shows the adapter agrees with the reference as transcribed. It does not show that
Healthie answers the way the fixtures do (see ADR 0018).
