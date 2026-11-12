# Population generation cases

The pipeline makes it easy to create synthetic populations and simulations
for other regions than Île-de-France. In any case, we recommend to first
follow instructions to set up a [synthetic population for Île-de-France](../population/population_summary.md)
and (if desired) the [respective simulation](../simulation/simulation_summary.md).

## Generic process

To set up a new region, you need to download the following datasets for the
correct departments or regions:

- [Buildings database (BD TOPO)](https://geoservices.ign.fr/bdtopo)
- [Adresses database (BAN)](https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/)
- [OpenStreetMap data](https://download.geofabrik.de/europe/france.html)
  (when running the simulation only)
- [GTFS data](https://transport.data.gouv.fr/)
  (when running the simulation only)

You have several options for the Household Travel Survey (HTS):

- Use a **national survey** (ENTD or EMP), as described in the
  [Île-de-France case](../population/population_data.md#a-national-household-travel-survey-entd-2008).
- Use a **dedicated implementation for a regional survey**
  (see [specific cases](specific-cases) below).
- Import any regional survey from
  [**MobiSurvStd**](https://mobisurvstd.github.io/MobiSurvStd/surveys.html).

Create or update the configuration file (e.g., `config_myregion.yml`) to
specify the region or departments, data paths, and HTS source.
Below is a template with explanations for each field:

```yaml
config:
  # Define either the regions or departments to be processed.
  # Ensure BD TOPO and BAN files are available in the specified directories.
  regions: []      # List of regions (e.g., ["94"])
  departments: []  # List of departments (e.g., ["01", "38", "42", "69"])

  # Optional: Add a prefix to output files for easy identification.
  output_prefix: myregion_

  # Update paths to region-specific data (relative to `data_path`).
  ban_path: ban_myregion
  bdtopo_path: bdtopo_myregion
  osm_path: osm_myregion
  gtfs_path: gtfs_myregion

  # Define the HTS source: entd, emp, mobisurvstd, or a regional implementation.
  hts: mobisurvstd

  # If using MobiSurvStd, specify the path to the survey zipfile or directory
  # (relative to `data_path`).
  mobisurvstd:
    path: my_survey.zip
```

## Choosing the perimeter

The pipeline selects its perimeter by regions or departments. Commuting flows
that cross this perimeter are lost, which matters when a city is close to a
department boundary: its commuters partly live in the neighbouring department.

The script `scripts/zone.py` helps to choose the perimeter around a target
municipality. Based on the census work and education flows (MOBPRO and MOBSCO)
that the pipeline already uses, it finds the zone that cuts the fewest flows
for a given size budget, and compares it with a selection by department:

```bash
python scripts/zone.py config.yml 84007 --budget 350000
```

```
Perimeter               Municipalities    Persons  Cut flows  Cut flows of the target
Zone                               116    348,813      18.8%                    22.6%
Department 84                      151    341,886      24.5%                    47.1%
Departments 13, 30, 84             636  2,098,487       8.9%                     6.3%

The zone lies in the following departments:
  departments: ['13', '30', '84']
```

- The first argument is your configuration file, which is used to locate the
  input data (`data_path`). The second one is the code of the target
  municipality.
- `--budget` is the maximum size of the zone, counted in persons with a work or
  education flow (the active and studying population, not the total
  population). Without a budget, the zone grows until adding municipalities no
  longer pays off.
- *Cut flows* is the share of the flows touching the perimeter that cross it,
  *Cut flows of the target* is the same share for the flows between the target
  and another municipality.
- `--output-path` writes the municipalities of the zone to a CSV file, and
  `--geometry-path` writes its shape to a GeoJSON file, which can for instance
  be used as the extent of the [scenario cutter](../simulation/scenario_cutter.md).

The zone minimises the flows of the target that are cut, plus the other cut
flows weighted by `--alpha` (0.1 by default), plus a price per person in the
zone. This is a minimum cut problem, which is solved exactly.

## Specific cases

For detailed instructions on setting up specific regions, refer to the
following guides:

```{toctree}
:titlesonly:

corsica.md
lyon.md
nantes.md
toulouse.md
```
