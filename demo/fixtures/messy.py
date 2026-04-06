def clean_dict(data):
    """Filter out None values, strip whitespace from non-empty strings, pass through all other types unchanged."""
    result = {}
    for key, value in data.items():
        if value is not None:
            if isinstance(value, str) and len(value.strip()) > 0:
                result[key] = value.strip()
            else:
                result[key] = value
    return result


if __name__ == "__main__":
    data = {"name": "  Alice  ", "age": 30, "bio": "", "city": None}
    print(clean_dict(data))  # should print {'name': 'Alice', 'age': 30}
