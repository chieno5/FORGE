void matmul(const int a[32][32], const int b[32][32], int c[32][32]) {
    for (int i = 0; i < 32; i++) {
        for (int j = 0; j < 32; j++) {
            int sum = 0;
            for (int k = 0; k < 32; k++) {
                sum += a[i][k] * b[k][j];
            }
            c[i][j] = sum;
        }
    }
}
