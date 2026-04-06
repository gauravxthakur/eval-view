def p(d):
    r = {}
    for k in d:
        if d[k] != None:
            if type(d[k]) == str:
                if len(d[k]) > 0:
                    r[k] = d[k].strip()
                else:
                    pass
            else:
                r[k] = d[k]
    return r


if __name__ == "__main__":
    data = {"name": "  Alice  ", "age": 30, "bio": "", "city": None}
    print(p(data))  # should print {'name': 'Alice', 'age': 30}
