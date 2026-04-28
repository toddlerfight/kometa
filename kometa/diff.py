from datetime import date


def compute_diff(komga_books: list, metron_issues: list, today: date) -> dict:
    owned = {float(b["metadata"]["numberSort"]) for b in komga_books
             if b["metadata"]["numberSort"] is not None}

    missing, upcoming = [], []
    for issue in metron_issues:
        num = float(issue["number"])
        store_date = date.fromisoformat(issue["store_date"]) if issue["store_date"] else None

        if num in owned:
            continue
        if store_date and store_date > today:
            upcoming.append((num, issue["store_date"]))
        else:
            missing.append(num)

    return {
        "owned": sorted(owned),
        "missing": sorted(missing),
        "upcoming": sorted(upcoming, key=lambda x: x[0]),
    }
