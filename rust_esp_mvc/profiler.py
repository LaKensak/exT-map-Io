import time

t0 = time.perf_counter()

# lecture

t1 = time.perf_counter()

# traitement

t2 = time.perf_counter()

# rendu / calcul

t3 = time.perf_counter()

print(
    f"lecture={(t1-t0)*1000:.3f}ms | "
    f"traitement={(t2-t1)*1000:.3f}ms | "
    f"reste={(t3-t2)*1000:.3f}ms"
)