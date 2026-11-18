from __future__ import annotations

from dataclasses import dataclass

from src.config_loader import geo_registry


@dataclass(frozen=True)
class Country:
    name: str
    continent_code: str
    ensun_location: str
    location_depth: str = "country"
    ensun_aliases: tuple[str, ...] = ()
    phase: int | None = None

    @property
    def ensun_country_name(self) -> str:
        """Country part of the ensun location triple (e.g. "Côte d'Ivoire")."""
        return self.ensun_location.split(",")[0]


@dataclass(frozen=True)
class Continent:
    code: str
    ensun_code: str
    countries: list[Country]


def load_continents() -> list[Continent]:
    data = geo_registry().get("continents", {})
    continents: list[Continent] = []
    for code, block in data.items():
        countries = [
            Country(
                name=c["name"],
                continent_code=code,
                ensun_location=c["ensun_location"],
                location_depth=c.get("location_depth", "country"),
                ensun_aliases=tuple(c.get("ensun_aliases", [])),
                phase=c.get("phase"),
            )
            for c in block.get("countries", [])
        ]
        continents.append(
            Continent(code=code, ensun_code=block.get("ensun_code", code), countries=countries)
        )
    return continents


def find_continent(continent_code: str) -> Continent | None:
    code = continent_code.strip().upper()
    return next((c for c in load_continents() if c.code == code), None)


def find_country(continent_code: str, country_name: str) -> Country | None:
    wanted = country_name.strip().lower()
    continent = find_continent(continent_code)
    if not continent:
        return None
    for country in continent.countries:
        names = [country.name, *country.ensun_aliases]
        if any(n.lower() == wanted for n in names):
            return country
    return None


def _phase_label(country: Country) -> str:
    return f"  (Phase {country.phase})" if country.phase else ""


def pick_continent_interactive() -> Continent:
    continents = load_continents()
    print("\n=== Ensun Agri Scraper ===\n")
    print("Select a continent:")
    for idx, continent in enumerate(continents, start=1):
        print(f"  {idx}. {continent.code} ({len(continent.countries)} countries)")
    while True:
        choice = input("\nEnter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(continents):
            return continents[int(choice) - 1]
        print("Invalid choice. Try again.")


def pick_country_interactive(continent: Continent) -> Country:
    print(f"\nSelect a country ({continent.code}):")
    for idx, country in enumerate(continent.countries, start=1):
        print(f"  {idx}. {country.name}{_phase_label(country)}")
    while True:
        choice = input("\nEnter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(continent.countries):
            return continent.countries[int(choice) - 1]
        print("Invalid choice. Try again.")
