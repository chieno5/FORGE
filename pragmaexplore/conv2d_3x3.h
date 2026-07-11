#pragma once

#define CONV2D_ROWS 64
#define CONV2D_COLS 64
#define CONV2D_K 3

void conv2d_3x3(
    const int input[CONV2D_ROWS][CONV2D_COLS],
    const int kernel[CONV2D_K][CONV2D_K],
    int output[CONV2D_ROWS][CONV2D_COLS],
    int smooth[CONV2D_ROWS][CONV2D_COLS],
    int edge[CONV2D_ROWS][CONV2D_COLS]
);
