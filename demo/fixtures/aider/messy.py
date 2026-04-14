def p(d):
    r = {}
    for k in d:
        v = d[k]
        if v is None:
            pass
        else:
            if type(v) == str:
                if len(v.strip()) > 0:
                    r[k] = v.strip()
                else:
                    pass
            else:
                r[k] = v
    return r


if __name__ == "__main__":
    data = {"name": "  Alice  ", "age": 30, "bio": "", "city": None}
    print(p(data))  # should print {'name': 'Alice', 'age': 30}
