#include "conv2d_3x3.h"

void conv2d_3x3(
    const int input[CONV2D_ROWS][CONV2D_COLS],
    const int kernel[CONV2D_K][CONV2D_K],
    int output[CONV2D_ROWS][CONV2D_COLS],
    int smooth[CONV2D_ROWS][CONV2D_COLS],
    int edge[CONV2D_ROWS][CONV2D_COLS]
) {
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=kernel offset=slave bundle=gmem1
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem2
#pragma HLS INTERFACE m_axi port=smooth offset=slave bundle=gmem3
#pragma HLS INTERFACE m_axi port=edge offset=slave bundle=gmem4
#pragma HLS INTERFACE s_axilite port=input bundle=CTRL
#pragma HLS INTERFACE s_axilite port=kernel bundle=CTRL
#pragma HLS INTERFACE s_axilite port=output bundle=CTRL
#pragma HLS INTERFACE s_axilite port=smooth bundle=CTRL
#pragma HLS INTERFACE s_axilite port=edge bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return bundle=CTRL

    for (int r = 0; r < CONV2D_ROWS; r++) {
        for (int c = 0; c < CONV2D_COLS; c++) {
            int acc = 0;
            int box_acc = 0;
            for (int kr = 0; kr < CONV2D_K; kr++) {
                for (int kc = 0; kc < CONV2D_K; kc++) {
                    int rr = r + kr - 1;
                    int cc = c + kc - 1;
                    int pix = 0;
                    if (rr >= 0 && rr < CONV2D_ROWS && cc >= 0 && cc < CONV2D_COLS) {
                        pix = input[rr][cc];
                    }
                    acc += pix * kernel[kr][kc];
                    box_acc += pix;
                }
            }

            int left = (c > 0) ? input[r][c - 1] : 0;
            int right = (c < CONV2D_COLS - 1) ? input[r][c + 1] : 0;
            int up = (r > 0) ? input[r - 1][c] : 0;
            int down = (r < CONV2D_ROWS - 1) ? input[r + 1][c] : 0;
            int center = input[r][c];

            output[r][c] = acc;
            smooth[r][c] = box_acc / 9;
            edge[r][c] = 4 * center - left - right - up - down;
        }
    }
}
