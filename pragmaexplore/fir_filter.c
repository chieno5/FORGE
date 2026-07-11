#include "fir_filter.h"

void fir_filter(
    const int input[FIR_MAX_N],
    const int coeff[FIR_TAPS],
    int output[FIR_MAX_N],
    int moving_avg[FIR_MAX_N],
    int diff_out[FIR_MAX_N],
    int n
) {
#pragma HLS INTERFACE m_axi port=input offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=coeff offset=slave bundle=gmem1
#pragma HLS INTERFACE m_axi port=output offset=slave bundle=gmem2
#pragma HLS INTERFACE m_axi port=moving_avg offset=slave bundle=gmem3
#pragma HLS INTERFACE m_axi port=diff_out offset=slave bundle=gmem4
#pragma HLS INTERFACE s_axilite port=input bundle=CTRL
#pragma HLS INTERFACE s_axilite port=coeff bundle=CTRL
#pragma HLS INTERFACE s_axilite port=output bundle=CTRL
#pragma HLS INTERFACE s_axilite port=moving_avg bundle=CTRL
#pragma HLS INTERFACE s_axilite port=diff_out bundle=CTRL
#pragma HLS INTERFACE s_axilite port=n bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return bundle=CTRL

    int shift[FIR_TAPS];
    int avg_shift[4];
    int prev = 0;

    if (n > FIR_MAX_N) {
        n = FIR_MAX_N;
    }

    for (int t = 0; t < FIR_TAPS; t++) {
        shift[t] = 0;
    }
    for (int t = 0; t < 4; t++) {
        avg_shift[t] = 0;
    }

    for (int i = 0; i < n; i++) {
        for (int t = FIR_TAPS - 1; t > 0; t--) {
            shift[t] = shift[t - 1];
        }
        shift[0] = input[i];

        for (int t = 3; t > 0; t--) {
            avg_shift[t] = avg_shift[t - 1];
        }
        avg_shift[0] = input[i];

        int acc = 0;
        for (int t = 0; t < FIR_TAPS; t++) {
            acc += shift[t] * coeff[t];
        }

        int avg_acc = 0;
        for (int t = 0; t < 4; t++) {
            avg_acc += avg_shift[t];
        }

        output[i] = acc;
        moving_avg[i] = avg_acc / 4;
        diff_out[i] = input[i] - prev;
        prev = input[i];
    }
}
