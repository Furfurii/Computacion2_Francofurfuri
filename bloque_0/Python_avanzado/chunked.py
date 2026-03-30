def chunked(iterable, size):
    bloque = []

    for elemento in iterable:
        bloque.append(elemento)

        if len(bloque) == size:
            yield bloque
            bloque = []

    if bloque:
        yield bloque


# ===== pruebas =====

print(list(chunked([1, 2, 3, 4, 5, 6, 7], 3)))
# [[1, 2, 3], [4, 5, 6], [7]]

print(list(chunked("abcdefg", 2)))
# [['a', 'b'], ['c', 'd'], ['e', 'f'], ['g']]

for bloque in chunked(range(10), 4):
    print(bloque)