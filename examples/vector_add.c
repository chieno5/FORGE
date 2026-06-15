void vector_add(const int a[1024], const int b[1024], int c[1024], int n) {
    for (int i = 0; i < n; i++) {
        c[i] = a[i] + b[i];
    }
}
