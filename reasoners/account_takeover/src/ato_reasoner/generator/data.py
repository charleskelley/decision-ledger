"""Static data pools for the ATO Reasoner synthetic event generator.

All geographic coordinates, ASN identifiers, IP ranges, and user agent
strings used by the factory are defined here. Nothing in this module
performs I/O or calls external services — all data is hardcoded.

Geographic regions map to the GeoMobility config dimensions:
    - STATIC: single home city, no movement
    - REGIONAL: cities within the same region
    - ERRATIC: cities drawn from across all regions

IP ranges for known-bad ASNs use RFC 5737 documentation prefixes
(192.0.2.x, 198.51.100.x, 203.0.113.x) — explicitly non-routable,
clearly synthetic, never assigned to real infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random

# ---------------------------------------------------------------------------
# Geographic data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CityRecord:
    """A city entry in the geographic data pool.

    Attributes:
        city: City name.
        country: ISO 3166-1 alpha-2 country code.
        latitude: Decimal degrees latitude.
        longitude: Decimal degrees longitude.
        region: Broad geographic region for mobility routing.
    """

    city: str
    country: str
    latitude: float
    longitude: float
    region: str


GEO_POOL: list[CityRecord] = [
    # North America
    CityRecord("New York", "US", 40.7128, -74.0060, "north_america"),
    CityRecord("Los Angeles", "US", 34.0522, -118.2437, "north_america"),
    CityRecord("Chicago", "US", 41.8781, -87.6298, "north_america"),
    CityRecord("Toronto", "CA", 43.6532, -79.3832, "north_america"),
    CityRecord("Miami", "US", 25.7617, -80.1918, "north_america"),
    # Europe
    CityRecord("London", "GB", 51.5074, -0.1278, "europe"),
    CityRecord("Paris", "FR", 48.8566, 2.3522, "europe"),
    CityRecord("Berlin", "DE", 52.5200, 13.4050, "europe"),
    CityRecord("Madrid", "ES", 40.4168, -3.7038, "europe"),
    CityRecord("Amsterdam", "NL", 52.3676, 4.9041, "europe"),
    CityRecord("Moscow", "RU", 55.7558, 37.6173, "europe"),
    # Asia
    CityRecord("Tokyo", "JP", 35.6762, 139.6503, "asia"),
    CityRecord("Beijing", "CN", 39.9042, 116.4074, "asia"),
    CityRecord("Singapore", "SG", 1.3521, 103.8198, "asia"),
    CityRecord("Mumbai", "IN", 19.0760, 72.8777, "asia"),
    CityRecord("Seoul", "KR", 37.5665, 126.9780, "asia"),
    CityRecord("Bangkok", "TH", 13.7563, 100.5018, "asia"),
    # South America
    CityRecord("São Paulo", "BR", -23.5505, -46.6333, "south_america"),
    CityRecord("Buenos Aires", "AR", -34.6037, -58.3816, "south_america"),
    CityRecord("Bogotá", "CO", 4.7110, -74.0721, "south_america"),
    # Africa
    CityRecord("Lagos", "NG", 6.5244, 3.3792, "africa"),
    CityRecord("Nairobi", "KE", -1.2921, 36.8219, "africa"),
    CityRecord("Cairo", "EG", 30.0444, 31.2357, "africa"),
    # Oceania
    CityRecord("Sydney", "AU", -33.8688, 151.2093, "oceania"),
    CityRecord("Melbourne", "AU", -37.8136, 144.9631, "oceania"),
    # Middle East
    CityRecord("Dubai", "AE", 25.2048, 55.2708, "middle_east"),
    CityRecord("Istanbul", "TR", 41.0082, 28.9784, "middle_east"),
]

# Pre-group by region for fast lookup
_GEO_BY_REGION: dict[str, list[CityRecord]] = {}
for _city in GEO_POOL:
    _GEO_BY_REGION.setdefault(_city.region, []).append(_city)

ALL_REGIONS: list[str] = list(_GEO_BY_REGION)


def cities_for_region(region: str) -> list[CityRecord]:
    """Return all cities for the given region.

    Args:
        region: Region identifier (e.g. "north_america", "europe").

    Returns:
        List of CityRecord entries for that region.
    """
    return _GEO_BY_REGION.get(region, [])


def other_regions(region: str) -> list[str]:
    """Return all regions except the given one.

    Used for impossible travel: consecutive events must be in a different
    region so that travel speed between them is physically impossible.

    Args:
        region: The region to exclude.

    Returns:
        All regions except the specified one.
    """
    return [r for r in ALL_REGIONS if r != region]


# ---------------------------------------------------------------------------
# ASN pools
# ---------------------------------------------------------------------------

# Residential ISP ASNs (real, well-known)
RESIDENTIAL_ASNS: list[str] = [
    "AS7922",  # Comcast
    "AS20001",  # Charter / Spectrum
    "AS7018",  # AT&T
    "AS6167",  # Verizon
    "AS2856",  # BT
    "AS3320",  # Deutsche Telekom
    "AS1221",  # Telstra
    "AS9443",  # Singtel
    "AS3786",  # LG Uplus
    "AS4134",  # Chinanet
    "AS9808",  # China Mobile
    "AS3462",  # Chunghwa Telecom
    "AS17676",  # SoftBank
    "AS5483",  # Magyar Telekom
    "AS8529",  # Omantel
]

# Corporate / datacenter ASNs (real, well-known)
CORPORATE_ASNS: list[str] = [
    "AS15169",  # Google
    "AS16509",  # Amazon AWS
    "AS8075",  # Microsoft Azure
    "AS13335",  # Cloudflare
    "AS14061",  # DigitalOcean
]

# Synthesized known-bad ASNs — IANA documentation range (AS64496-AS64511),
# explicitly reserved for documentation use; never assigned to real operators.
KNOWN_BAD_ASNS: list[str] = [
    "AS64496",
    "AS64497",
    "AS64498",
    "AS64499",
    "AS64500",
    "AS64501",
    "AS64502",
    "AS64503",
]

# ---------------------------------------------------------------------------
# User agent pools
# ---------------------------------------------------------------------------

# Standard browser / mobile user agents
USER_AGENTS: list[str] = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",  # noqa: E501
    # Chrome / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",  # noqa: E501
    # Firefox / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Firefox / Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Safari / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",  # noqa: E501
    # Edge / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",  # noqa: E501
    # Chrome / Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.43 Mobile Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",  # noqa: E501
    # Safari / iPhone
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",  # noqa: E501
    # Safari / iPad
    "Mozilla/5.0 (iPad; CPU OS 17_1_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",  # noqa: E501
    # Opera
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",  # noqa: E501
]

# Service / API client user agents — used for SSO / high-velocity legitimate
SERVICE_USER_AGENTS: list[str] = [
    "python-requests/2.31.0",
    "python-httpx/0.27.0",
    "okhttp/4.12.0",
    "axios/1.6.2",
    "Go-http-client/2.0",
    "Java/17.0.9",
]

# ---------------------------------------------------------------------------
# IP generation helpers
# ---------------------------------------------------------------------------

# RFC 5737 documentation prefixes — non-routable, clearly synthetic
_KNOWN_BAD_PREFIXES: list[tuple[int, int, int]] = [
    (192, 0, 2),
    (198, 51, 100),
    (203, 0, 113),
]


def random_ipv4(rng: random.Random, *, known_bad: bool = False) -> str:
    """Generate a synthetic IPv4 address.

    Args:
        rng: Seeded random instance for reproducibility.
        known_bad: If True, uses RFC 5737 documentation prefixes — explicitly
            non-routable ranges that signal synthesized "bad" infrastructure.

    Returns:
        Dotted-decimal IPv4 string (e.g. "203.0.113.42").
    """
    if known_bad:
        prefix = rng.choice(_KNOWN_BAD_PREFIXES)
        return f"{prefix[0]}.{prefix[1]}.{prefix[2]}.{rng.randint(1, 254)}"
    # Random public IPv4: first octet 1-223 avoids multicast/reserved blocks
    return (
        f"{rng.randint(1, 223)}"
        f".{rng.randint(0, 255)}"
        f".{rng.randint(0, 255)}"
        f".{rng.randint(1, 254)}"
    )
