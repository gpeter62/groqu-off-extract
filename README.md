# groQu – Open Food Facts sports nutrition extract

A subset of the [Open Food Facts](https://world.openfoodfacts.org) database: sports nutrition
products (protein powders, protein bars, shakes, energy and recovery products) of brands sold in
Hungary and Central Europe, as used by the groQu app's built-in food catalogue.

- `off-sports-extract.json` – the extract (retrieved 2026-10-07 and 2026-10-08 via the Open Food Facts API).
- `build-off-extract.py` – the script that produced it (Python standard library only).

The app bundles the products that have known energy and either at least 10 g protein per 100 g
or an energy/isotonic/recovery name.

## Licence

This extract is a derivative database of Open Food Facts and is made available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Individual
contents of the database are available under the
[Database Contents License (DbCL) 1.0](https://opendatacommons.org/licenses/dbcl/1-0/).

© Open Food Facts contributors, https://world.openfoodfacts.org

The data is provided as is, without warranty. For packaged products the label is what counts.
