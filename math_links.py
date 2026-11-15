"""Ready-made list of advanced-mathematics websites for one-click adding to Website Sources."""
from web_sources import add_or_refresh_source, list_sources, validate_url

# (group, title, url)
MATH_LINKS = [
    ("Reference", "Wolfram MathWorld", "https://mathworld.wolfram.com/"),
    ("Reference", "Encyclopedia of Mathematics", "https://encyclopediaofmath.org/wiki/Main_Page"),
    ("Reference", "ProofWiki", "https://proofwiki.org/wiki/Main_Page"),
    ("Reference", "NIST Digital Library of Mathematical Functions", "https://dlmf.nist.gov/"),
    ("Courses & notes", "Paul's Online Math Notes (Calculus, Diff. Eq., Linear Algebra)", "https://tutorial.math.lamar.edu/"),
    ("Courses & notes", "MIT OpenCourseWare - Mathematics", "https://ocw.mit.edu/courses/mathematics/"),
    ("Courses & notes", "LibreTexts Mathematics", "https://math.libretexts.org/"),
    ("Courses & notes", "OpenStax Math textbooks", "https://openstax.org/subjects/math"),
    ("Topic overviews", "Calculus", "https://en.wikipedia.org/wiki/Calculus"),
    ("Topic overviews", "Linear algebra", "https://en.wikipedia.org/wiki/Linear_algebra"),
    ("Topic overviews", "Differential equations", "https://en.wikipedia.org/wiki/Differential_equation"),
    ("Topic overviews", "Real analysis", "https://en.wikipedia.org/wiki/Real_analysis"),
    ("Topic overviews", "Complex analysis", "https://en.wikipedia.org/wiki/Complex_analysis"),
    ("Topic overviews", "Abstract algebra", "https://en.wikipedia.org/wiki/Abstract_algebra"),
    ("Topic overviews", "Number theory", "https://en.wikipedia.org/wiki/Number_theory"),
    ("Topic overviews", "Probability theory", "https://en.wikipedia.org/wiki/Probability_theory"),
    ("Topic overviews", "Topology", "https://en.wikipedia.org/wiki/Topology"),
    ("Topic overviews", "Numerical analysis", "https://en.wikipedia.org/wiki/Numerical_analysis"),
    ("Topic overviews", "Discrete mathematics", "https://en.wikipedia.org/wiki/Discrete_mathematics"),
]


def add_many_sources(db_path: str, urls: list, username: str) -> tuple[bool, str]:
    """Fetch and index several websites one after another (runs as a background job). Never raises."""
    try:
        existing = set()
        for src in list_sources(db_path):
            try:
                existing.add(src["url"])
            except Exception:
                pass
        added, skipped, failed = [], [], []
        for url in urls:
            ok, norm = validate_url(url)
            if not ok:
                failed.append(f"{url} ({norm})")
                continue
            if norm in existing:
                skipped.append(norm)
                continue
            ok, msg = add_or_refresh_source(db_path, norm, username)
            (added if ok else failed).append(norm if ok else f"{norm} ({msg})")
        parts = [f"Added {len(added)} website(s)."]
        if skipped:
            parts.append(f"{len(skipped)} already in the list (skipped).")
        if failed:
            parts.append("Could not fetch: " + "; ".join(failed))
        return (len(added) > 0 or (not failed and bool(skipped))), " ".join(parts)
    except Exception as e:
        return False, f"Adding websites failed: {e}"
