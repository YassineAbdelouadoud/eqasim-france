from typing import Annotated, Optional
from pathlib import Path

import typer
from rich import print
from rich.table import Table
from rich.console import Console

import glob, os, yaml, zipfile

import numpy as np
import pandas as pd
import polars as pl

from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import maximum_flow, breadth_first_order

"""
Finds the zone around a target municipality that cuts the fewest commuting
flows for a given size budget, based on the census work and education flows
(MOBPRO and MOBSCO) that the pipeline already uses.

Selecting the perimeter of a use case by department can cut a large share of
the flows of a city that is close to a department boundary. This script
measures that share, and proposes a zone that is built from the flows.

Among the zones Z containing the target, the script minimises

    flows of the target cut by Z + alpha * other flows cut by Z + mu * size(Z)

which is a minimum s-t cut problem and is solved exactly. mu is the price of
adding one person to the zone: decreasing it yields larger, nested zones, each
of which is optimal for its own size. The zone for a budget is the largest one
that does not exceed it.

Sizes are counted in persons with a work or education flow, i.e. the active
and studying population, not the total population.
"""

TEMPORARY_PATH = Path(".script_data")

HELP_CONFIG_PATH = "The path to your config.yml, used to locate the input data"
HELP_TARGET = "The municipality code of the target (e.g. 84007 for Avignon)"
HELP_BUDGET = "Maximum size of the zone, in persons with a work or education flow"
HELP_ALPHA = "Weight of the cut flows that do not start or end in the target"
HELP_DEPARTMENT_SHARE = "Share of the persons of the zone below which a department is not listed"
HELP_OUTPUT_PATH = "Optional path to a CSV file to write the municipalities of the zone"
HELP_GEOMETRY_PATH = "Optional path to a GeoJSON file to write the shape of the zone (e.g. for the scenario cutter)"

# Flows are scaled to integers for the maximum flow solver
SCALE = 10
INFINITE = 2**31 - 1


def load_codes(data_path, codes_path, codes_xlsx):
    with zipfile.ZipFile(data_path / codes_path) as archive:
        with archive.open(codes_xlsx) as f:
            df_codes = pd.read_excel(f, skiprows = 5, sheet_name = "Emboitements_IRIS", dtype = str)

    df_codes = df_codes.rename(columns = {
        "DEPCOM": "commune_id",
        "LIBCOM": "commune_name",
        "DEP": "departement_id"
    })

    if not "commune_name" in df_codes:
        df_codes["commune_name"] = df_codes["commune_id"]

    df_codes = df_codes[["commune_id", "commune_name", "departement_id"]]
    return df_codes.drop_duplicates("commune_id").set_index("commune_id")


def load_flows(data_path, od_pro_path, od_sco_path):
    """
    Loads the work and education flows between municipalities. Municipal
    districts (Paris, Lyon, Marseille) are kept separate, as in the pipeline.
    """
    df_flows = []

    for path, destination in ((od_pro_path, "DCLT"), (od_sco_path, "DCETUF")):
        df = pl.read_parquet(data_path / path, columns = ["COMMUNE", "ARM", destination, "IPONDI"]).cast({
            "COMMUNE": pl.String, "ARM": pl.String, destination: pl.String, "IPONDI": pl.Float64
        })

        df = df.select(
            pl.when(pl.col("ARM").str.starts_with("Z")).then(pl.col("COMMUNE")).otherwise(pl.col("ARM")).alias("origin_id"),
            pl.col(destination).alias("destination_id"),
            pl.col("IPONDI").alias("weight")
        )

        df_flows.append(df)

    return pl.concat(df_flows).group_by(["origin_id", "destination_id"]).agg(pl.col("weight").sum()).to_pandas()


class FlowGraph:
    """Flows between municipalities, prepared for the search of a zone."""

    def __init__(self, df_flows, municipalities):
        self.municipalities = np.asarray(municipalities)
        self.index = {municipality: index for index, municipality in enumerate(self.municipalities)}
        count = len(self.municipalities)

        origin = df_flows["origin_id"].map(self.index).values
        destination = df_flows["destination_id"].map(self.index).values
        weight = df_flows["weight"].values
        internal = origin == destination

        # Persons living in each municipality
        self.sizes = np.bincount(origin, weights = weight, minlength = count)

        # Flows that stay inside each municipality
        self.internal = np.bincount(origin[internal], weights = weight[internal], minlength = count)

        # Symmetric matrix of the flows exchanged by two municipalities, in both directions
        origin, destination, weight = origin[~internal], destination[~internal], weight[~internal]
        self.matrix = coo_matrix(
            (np.r_[weight, weight], (np.r_[origin, destination], np.r_[destination, origin])),
            shape = (count, count)).tocsr()
        self.matrix.sum_duplicates()
        self.exchanged = np.asarray(self.matrix.sum(axis = 1)).ravel()

    def find_zone(self, target, penalty, alpha):
        """
        Returns the zone Z containing the target that minimises

            flows of the target cut by Z + alpha * other flows cut by Z + penalty * size(Z)

        as a boolean mask over the municipalities. This is the source side of
        a minimum cut in a graph where the target is attached to the source,
        and every municipality to the sink with a capacity of penalty * size.
        """
        count = len(self.municipalities)
        source, sink = count, count + 1

        flows = self.matrix.tocoo()
        touching = (flows.row == target) | (flows.col == target)
        capacity = np.where(touching, 1.0, alpha) * flows.data

        rows = np.r_[flows.row, np.arange(count), source]
        columns = np.r_[flows.col, np.full(count, sink), target]
        capacities = np.r_[capacity, penalty * self.sizes, 0.0] * SCALE
        capacities = np.minimum(np.round(capacities), INFINITE).astype(np.int32)
        capacities[-1] = INFINITE

        graph = coo_matrix((capacities, (rows, columns)), shape = (count + 2, count + 2)).tocsr()
        flow = maximum_flow(graph, source, sink).flow

        # The zone is what can still be reached from the source
        residual = graph - flow
        residual.data[residual.data < 0] = 0
        residual.eliminate_zeros()
        reachable = breadth_first_order(residual, source, directed = True, return_predecessors = False)

        zone = np.zeros(count, dtype = bool)
        zone[reachable[reachable < count]] = True
        return zone

    def find_largest_zone(self, target, budget, alpha, highest = 4.0, lowest = 0.002, steps = 6):
        """
        Returns the largest optimal zone around the target that does not
        exceed the budget. Without a budget, the zone obtained for the lowest
        penalty is returned.
        """
        budget = np.inf if budget is None else budget

        zone = np.zeros(len(self.municipalities), dtype = bool)
        zone[target] = True

        # Decrease the penalty until the zone exceeds the budget
        penalty = highest
        while penalty >= lowest:
            candidate = self.find_zone(target, penalty, alpha)
            if self.sizes[candidate].sum() > budget: break
            zone, fitting = candidate, penalty
            penalty /= 2
        else:
            return zone

        if penalty == highest: # even the smallest zone exceeds the budget
            return zone

        # Refine between the last fitting and the first exceeding penalty
        exceeding = penalty
        for _ in range(steps):
            penalty = np.sqrt(exceeding * fitting)
            candidate = self.find_zone(target, penalty, alpha)

            if self.sizes[candidate].sum() > budget:
                exceeding = penalty
            else:
                zone, fitting = candidate, penalty

        return zone

    def evaluate(self, zone, target):
        """Describes how well a zone (boolean mask) contains the flows."""
        inside = self.matrix[zone][:, zone].sum() / 2
        cut = self.exchanged[zone].sum() - 2 * inside

        target_flows = self.matrix[target]
        target_cut = target_flows.data[~zone[target_flows.indices]].sum()

        return {
            "municipalities": int(zone.sum()),
            "persons": self.sizes[zone].sum(),
            # among the flows with at least one end in the zone
            "cut_share": cut / (inside + self.internal[zone].sum() + cut),
            # among the flows between the target and another municipality
            "target_cut_share": target_cut / self.exchanged[target],
        }


def write_geometry(data_path, iris_path, municipalities, geometry_path):
    import geopandas as gpd
    import py7zr

    candidates = glob.glob("{}/*.7z".format(data_path / iris_path))
    if len(candidates) != 1:
        print("[red]Expected exactly one IRIS archive in {}[/red]".format(data_path / iris_path))
        raise typer.Exit(1)

    with py7zr.SevenZipFile(candidates[0]) as archive:
        contour_paths = [path for path in archive.getnames() if "LAMB93" in path and path.endswith(".gpkg")]
        archive.extract(TEMPORARY_PATH / "zone_iris", contour_paths)

    df_iris = gpd.read_file(TEMPORARY_PATH / "zone_iris" / contour_paths[0])[["code_insee", "geometry"]]
    df_iris = df_iris[df_iris["code_insee"].astype(str).isin(set(municipalities))]

    df_zone = gpd.GeoDataFrame({"geometry": [df_iris.union_all()]}, crs = df_iris.crs)
    df_zone.to_crs("EPSG:4326").to_file(geometry_path, driver = "GeoJSON")


def main(
        config_path: Annotated[Path, typer.Argument(help = HELP_CONFIG_PATH)],
        target: Annotated[str, typer.Argument(help = HELP_TARGET)],
        budget: Annotated[Optional[float], typer.Option(help = HELP_BUDGET)] = None,
        alpha: Annotated[float, typer.Option(help = HELP_ALPHA)] = 0.1,
        minimum_department_share: Annotated[float, typer.Option(help = HELP_DEPARTMENT_SHARE)] = 0.01,
        output_path: Annotated[Optional[Path], typer.Option(help = HELP_OUTPUT_PATH)] = None,
        geometry_path: Annotated[Optional[Path], typer.Option(help = HELP_GEOMETRY_PATH)] = None):

    print("Loading config ...")
    with open(config_path) as f:
        config = yaml.load(f, yaml.FullLoader)["config"]

    data_path = Path(config["data_path"])

    print("Loading zoning codes ...")
    df_codes = load_codes(data_path,
        config.get("codes_path", "codes_2024/reference_IRIS_geo2024.zip"),
        config.get("codes_xlsx", "reference_IRIS_geo2024.xlsx"))

    if not target in df_codes.index:
        print("[red]Unknown municipality: {}[/red]".format(target))
        raise typer.Exit(1)

    print("Loading work and education flows ...")
    df_flows = load_flows(data_path,
        config.get("od_pro_path", "rp_2022/RP2022_mobpro.parquet"),
        config.get("od_sco_path", "rp_2022/RP2022_mobsco.parquet"))

    known = df_flows["origin_id"].isin(df_codes.index) & df_flows["destination_id"].isin(df_codes.index)
    print("  Ignoring {:.1f}% of flows with an unknown municipality (e.g. abroad)".format(
        100 * df_flows.loc[~known, "weight"].sum() / df_flows["weight"].sum()))

    graph = FlowGraph(df_flows[known], df_codes.index.values)
    target_index = graph.index[target]

    print("Finding the zone around {} ({}) ...".format(df_codes.loc[target, "commune_name"], target))
    zone = graph.find_largest_zone(target_index, budget, alpha)

    # Compare with what can be selected by department
    departments = df_codes["departement_id"].values
    home_department = departments[target_index]

    # The zone may include a few remote municipalities that are tied to the
    # target: only keep the departments that hold a relevant part of the zone
    shares = pd.Series(graph.sizes[zone]).groupby(departments[zone]).sum() / graph.sizes[zone].sum()
    zone_departments = sorted(shares[shares >= minimum_department_share].index)
    minor_departments = sorted(shares[shares < minimum_department_share].index)

    perimeters = [
        ("Zone", zone),
        ("Department {}".format(home_department), departments == home_department)
    ]

    if len(zone_departments) > 1:
        perimeters.append(("Departments {}".format(", ".join(zone_departments)), np.isin(departments, zone_departments)))

    table = Table()
    for column in ("Perimeter", "Municipalities", "Persons", "Cut flows", "Cut flows of the target"):
        table.add_column(column, justify = "left" if column == "Perimeter" else "right")

    for name, mask in perimeters:
        result = graph.evaluate(mask, target_index)
        table.add_row(name,
            "{:d}".format(result["municipalities"]), "{:,.0f}".format(result["persons"]),
            "{:.1f}%".format(100 * result["cut_share"]), "{:.1f}%".format(100 * result["target_cut_share"]))

    Console().print(table)

    print("The zone lies in the following departments:")
    print("  departments: {}".format(zone_departments))

    if len(minor_departments) > 0:
        print("  (plus less than {:.0f}% of the zone in each of: {})".format(
            100 * minimum_department_share, ", ".join(minor_departments)))

    if output_path:
        print("Writing municipalities ...")
        df_codes[zone].reset_index()[["commune_id", "commune_name", "departement_id"]].to_csv(output_path, index = False)

    if geometry_path:
        print("Writing geometry ...")
        write_geometry(data_path, config.get("iris_path", "iris_2024"), graph.municipalities[zone], geometry_path)

    print("[green]Done![/green]")

if __name__ == "__main__":
    typer.run(main)
