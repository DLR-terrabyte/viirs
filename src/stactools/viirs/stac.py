import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import shapely.geometry
from pystac import Asset, Collection, Item, Summaries
from pystac.extensions.eo import EOExtension
from pystac.extensions.item_assets import AssetDefinition, ItemAssetsExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.scientific import ScientificExtension
from pystac.utils import datetime_to_str, make_absolute_href
from stactools.core.io import ReadHrefModifier
from stactools.core.utils import antimeridian, raster_footprint
from stactools.core.utils.antimeridian import Strategy

from stactools.viirs import constants
from stactools.viirs.fragment import STACFragments
from stactools.viirs.metadata import viirs_metadata
from stactools.viirs.utils import check_if_supported, find_extensions

logger = logging.getLogger(__name__)


def create_item(
    h5_href: str,
    cog_hrefs: Optional[List[str]] = None,
    read_href_modifier: Optional[ReadHrefModifier] = None,
    antimeridian_strategy: Strategy = Strategy.SPLIT,
    densification_factor: int = constants.FOOTPRINT_DENSIFICATION_FACTOR,
    simplification_tolerance: float = constants.FOOTPRINT_SIMPLIFICATION_TOLERANCE,
    use_data_footprint: bool = False,
) -> Item:
    """Creates a STAC Item from VIIRS data.

    Args:
        h5_href (str): href to an H5 (HDF5) file
        cog_hrefs (List[str], optional): Optional list of COG asset TIF hrefs
        read_href_modifier (Callable[[str], str], optional): An optional
            function to modify the href (e.g. to add a token to a url)
        antimeridian_strategy (Strategy, optional): Either split on -180 or
            normalize geometries so all longitudes are either positive or
            negative. Default is to split antimeridian geometries.
        densification_factor (int): Factor by which to increase the
            number of vertices on the extracted footprint geometry in the
            projected coordinate system. Densification mitigates the distortion
            error when reprojecting to WGS84 geodetic coordinates. Default is 10.
        simplification_tolerance (float): Maximum acceptable geodetic distance,
            in degrees, between the boundary of the simplified footprint geometry
            and the original, densified geometry vertices after reprojection.
            Default is 0.0006 degrees (~60m at the equator).
        use_data_footprint (bool): Flag to extract footprint geometry based on
            data existence rather than the raster outline.

    Returns:
        pystac.Item: A STAC Item representing the VIIRS data.
    """
    metadata = viirs_metadata(h5_href, read_href_modifier)
    fragments = STACFragments(metadata.product, metadata.production_julian_date)
    geometry = metadata.geometry(densification_factor, simplification_tolerance)

    item = Item(
        id=metadata.id,
        geometry=geometry,
        bbox=list(shapely.geometry.shape(geometry).bounds),
        datetime=metadata.acquisition_datetime,
        properties={
            "start_datetime": datetime_to_str(metadata.start_datetime),
            "end_datetime": datetime_to_str(metadata.end_datetime),
            "viirs:horizontal-tile": metadata.horizontal_tile,
            "viirs:vertical-tile": metadata.vertical_tile,
            "viirs:tile-id": metadata.tile_id,
        },
    )
    item.common_metadata.created = datetime.now(tz=timezone.utc)
    if "VJ" in item.id:
        item.common_metadata.platform = "jpss-1"
    else:
        item.common_metadata.platform = constants.PLATFORM

    item.common_metadata.instruments = constants.INSTRUMENT
    if fragments.gsd():
        item.common_metadata.gsd = fragments.gsd()

    properties = constants.HDF5_ASSET_PROPERTIES.copy()
    properties["href"] = make_absolute_href(h5_href)
    properties["created"] = datetime_to_str(metadata.production_datetime)
    item.add_asset(constants.HDF5_ASSET_KEY, Asset.from_dict(properties))

    if metadata.xml_href:
        properties = constants.METADATA_ASSET_PROPERTIES.copy()
        properties["href"] = make_absolute_href(metadata.xml_href)
        properties["created"] = datetime_to_str(metadata.production_datetime)
        item.add_asset(constants.METADATA_ASSET_KEY, Asset.from_dict(properties))

    if cog_hrefs:
        for href in cog_hrefs:
            basename = os.path.splitext(os.path.basename(href))[0]
            subdataset_name = basename.split("_", 1)[1]
            asset_dict = fragments.subdataset_dict(subdataset_name)
            asset_dict["href"] = make_absolute_href(href)
            item.add_asset(subdataset_name, Asset.from_dict(asset_dict))

    if metadata.cloud_cover:
        eo = EOExtension.ext(item, add_if_missing=True)
        eo.cloud_cover = metadata.cloud_cover

    projection = ProjectionExtension.ext(item, add_if_missing=True)
    projection.epsg = metadata.epsg
    if projection.epsg is None:
        projection.wkt2 = metadata.wkt2
    projection.transform = metadata.transform
    projection.shape = metadata.shape

    assets_dict = {k: v.to_dict() for k, v in item.assets.items()}
    extensions = find_extensions(assets_dict)
    item.stac_extensions.extend(extensions)
    item.stac_extensions = list(set(item.stac_extensions))
    item.stac_extensions.sort()

    antimeridian.fix_item(item, antimeridian_strategy)

    if use_data_footprint:
        if not cog_hrefs:
            logger.warning(
                "Cannot update Item geometry from valid raster data without "
                "COG hrefs. The raster outline was used instead."
            )
        elif not raster_footprint.update_geometry_from_asset_footprint(
            item,
            asset_names=constants.FOOTPRINT_DATA_ASSETS[metadata.product],
            precision=constants.FOOTPRINT_PRECISION,
            densification_factor=densification_factor,
            simplify_tolerance=simplification_tolerance,
        ):
            logger.warning(
                "Request to update Item geometry from valid raster data "
                "failed. The raster outline was used instead."
            )

    return item


def create_collection(product: str) -> Collection:
    """Creates a STAC Collection for a VIIRS product.

    Args:
        product (str): VIIRS product, e.g., 'VNP13A1'

    Returns:
        Collection: A STAC Collection for the product.
    """
    check_if_supported(product)

    fragments = STACFragments(product)

    summaries: Dict[str, Any] = {
        "instruments": constants.INSTRUMENT,
        "platform": [constants.PLATFORM],
    }
    if fragments.gsd() is not None:
        summaries["gsd"] = [fragments.gsd()]
    if fragments.collection_eo_bands_summary():
        summaries["eo:bands"] = fragments.collection_eo_bands_summary()

    collection_fragments = fragments.collection_dict()
    collection = Collection(
        id=collection_fragments["id"],
        title=collection_fragments["title"],
        description=collection_fragments["description"],
        license=collection_fragments["license"],
        keywords=collection_fragments["keywords"],
        providers=collection_fragments["providers"],
        extent=collection_fragments["extent"],
        summaries=Summaries(summaries),
    )
    collection.add_links(collection_fragments["links"])

    item_assets_dict = {
        constants.HDF5_ASSET_KEY: constants.HDF5_ASSET_PROPERTIES,
        constants.METADATA_ASSET_KEY: constants.METADATA_ASSET_PROPERTIES,
    }
    if product == constants.VIIRSProducts.VNP46A2:
        item_assets_dict.pop(constants.METADATA_ASSET_KEY)
    item_assets_dict.update(fragments.assets_dict())
    item_assets = {k: AssetDefinition(v) for k, v in item_assets_dict.items()}
    item_assets_ext = ItemAssetsExtension.ext(collection, add_if_missing=True)
    item_assets_ext.item_assets = item_assets

    ScientificExtension.add_to(collection)
    collection.extra_fields["sci:doi"] = collection_fragments["sci:doi"]
    collection.extra_fields["sci:citation"] = collection_fragments["sci:citation"]
    if "sci:publications" in collection_fragments:
        collection.extra_fields["sci:publications"] = collection_fragments[
            "sci:publications"
        ]

    extensions = find_extensions(item_assets_dict)
    collection.stac_extensions.extend(extensions)
    collection.stac_extensions = sorted(list(set(collection.stac_extensions)))

    return collection
