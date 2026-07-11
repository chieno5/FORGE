#pragma once

#define MATMUL_N 32

void matrix_multiply(
    const int a[MATMUL_N][MATMUL_N],
    const int b[MATMUL_N][MATMUL_N],
    const int bias[MATMUL_N],
    int c[MATMUL_N][MATMUL_N],
    int activated[MATMUL_N][MATMUL_N],
    int row_sum[MATMUL_N]
);
