#include "matrix_multiply.h"

void matrix_multiply(
    const int a[MATMUL_N][MATMUL_N],
    const int b[MATMUL_N][MATMUL_N],
    const int bias[MATMUL_N],
    int c[MATMUL_N][MATMUL_N],
    int activated[MATMUL_N][MATMUL_N],
    int row_sum[MATMUL_N]
) {
#pragma HLS INTERFACE m_axi port=a offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=b offset=slave bundle=gmem1
#pragma HLS INTERFACE m_axi port=bias offset=slave bundle=gmem2
#pragma HLS INTERFACE m_axi port=c offset=slave bundle=gmem3
#pragma HLS INTERFACE m_axi port=activated offset=slave bundle=gmem4
#pragma HLS INTERFACE m_axi port=row_sum offset=slave bundle=gmem5
#pragma HLS INTERFACE s_axilite port=a bundle=CTRL
#pragma HLS INTERFACE s_axilite port=b bundle=CTRL
#pragma HLS INTERFACE s_axilite port=bias bundle=CTRL
#pragma HLS INTERFACE s_axilite port=c bundle=CTRL
#pragma HLS INTERFACE s_axilite port=activated bundle=CTRL
#pragma HLS INTERFACE s_axilite port=row_sum bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return bundle=CTRL

    for (int i = 0; i < MATMUL_N; i++) {
        int row_acc = 0;
        for (int j = 0; j < MATMUL_N; j++) {
            int acc = 0;
            for (int k = 0; k < MATMUL_N; k++) {
                acc += a[i][k] * b[k][j];
            }
            c[i][j] = acc;
            int shifted = acc + bias[j];
            activated[i][j] = (shifted > 0) ? shifted : 0;
            row_acc += activated[i][j];
        }
        row_sum[i] = row_acc;
    }
}
