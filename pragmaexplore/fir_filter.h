#pragma once

#define FIR_TAPS 32
#define FIR_MAX_N 256

void fir_filter(
    const int input[FIR_MAX_N],
    const int coeff[FIR_TAPS],
    int output[FIR_MAX_N],
    int moving_avg[FIR_MAX_N],
    int diff_out[FIR_MAX_N],
    int n
);
