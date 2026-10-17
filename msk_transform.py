"""Build the intermediate MSK CRS used to recover NSPD coordinates."""
import math
import shlex

from qgis.core import QgsCoordinateReferenceSystem


GOST_2008 = (23.57, -140.95, -79.8, 0.0, 0.35, 0.79, -0.22)
GOST_2001 = "23.92,-141.27,-80.9,0,0.35,0.82,-0.12"


def make_gost2001_crs(target_crs):
    """Return an in-memory CRS; leave the selected final CRS unchanged.

    Only the known SK-42/Krassovsky transverse-Mercator definition with the
    2008 Helmert parameters is supported. Other datum transformations must not
    be silently replaced by a guessed one. No user CRS is registered in QGIS.
    """
    if target_crs is None or not target_crs.isValid():
        raise ValueError("Выберите МСК результата.")

    unsupported = (
        "Для выбранной системы автоматическое преобразование в МСК не поддерживается. "
        "Выберите МСК на эллипсоиде Красовского с проекцией Гаусса — Крюгера "
        "и параметрами перехода ГОСТ 2008."
    )
    try:
        tokens = shlex.split(target_crs.toProj())
        parameters = {}
        for token in tokens:
            if not token.startswith("+"):
                raise ValueError(unsupported)
            key, _, value = token[1:].partition("=")
            if key in parameters:
                raise ValueError(unsupported)
            parameters[key] = value
        # These CRS forms could contain additional transformations or override
        # the ellipsoid/units. They are outside the verified conversion path.
        allowed = {"proj", "lat_0", "lon_0", "k", "k_0", "x_0", "y_0",
                   "ellps", "towgs84", "units", "no_defs", "type"}
        if (parameters.keys() - allowed or target_crs.isGeographic()
                or parameters.get("proj") != "tmerc"
                or parameters.get("ellps") != "krass"
                or parameters.get("units") != "m"):
            raise ValueError(unsupported)
        shift = tuple(float(value) for value in parameters.get("towgs84", "").split(","))
        if len(shift) != 7 or not all(
                math.isfinite(value) and math.isclose(value, expected, rel_tol=0, abs_tol=1e-9)
                for value, expected in zip(shift, GOST_2008)):
            raise ValueError(unsupported)
    except (ValueError, TypeError) as error:
        raise ValueError(unsupported) from error

    definition = " ".join(
        "+towgs84=" + GOST_2001 if token.startswith("+towgs84=") else token
        for token in tokens
    )
    working_crs = QgsCoordinateReferenceSystem()
    if not working_crs.createFromProj(definition):
        raise ValueError("Не удалось создать промежуточную систему координат для выбранной МСК.")
    return working_crs
