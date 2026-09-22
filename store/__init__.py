"""The store FORMAT — one module per section group in den-spec `wire/store-v2.md`.

That document is the contract and its headings are this package's index: a reader who knows which
section they are after reads the heading, then opens the module of the same name.

  identity.py   Identity — keys, strings, str_off
  cards.py      Cards
  labels.py     Labels
  facets.py     Facets, and the FACETS-V2 publication gates that decide what reaches them
  scores.py     Scores, world, nouls, critique
  facts.py      Facts
  entities.py   Entities
  makers.py     The inverted maker index
  vectors.py    Vectors
  format.py     the wire primitives every group writes through — the dictionary, the section
                table, the header, and the fixed-point encodings
  inputs.py     reading the build's inputs, and the record of what it read
  build.py      the order the groups run in; implements no section itself

`scripts/v2/build_store.py` is the command line, and it owns the publication guards, which refuse on
tables that live there.
"""
