# Load the latest PriceRadar route/matching layer whenever the backend package starts.
# v217 imports the previous layers in order and replaces only the routes it owns.
from . import v217_similarity  # noqa: F401,E402
