#include "reduction_dot.h"

void reduction_dot(
    const int a[REDUCTION_MAX_N],
    const int b[REDUCTION_MAX_N],
    int result[5],
    int n
) {
#pragma HLS INTERFACE m_axi port=a offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=b offset=slave bundle=gmem1
#pragma HLS INTERFACE m_axi port=result offset=slave bundle=gmem2
#pragma HLS INTERFACE s_axilite port=a bundle=CTRL
#pragma HLS INTERFACE s_axilite port=b bundle=CTRL
#pragma HLS INTERFACE s_axilite port=result bundle=CTRL
#pragma HLS INTERFACE s_axilite port=n bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return bundle=CTRL

    if (n > REDUCTION_MAX_N) {
        n = REDUCTION_MAX_N;
    }

    int dot_acc = 0;
    int sum_acc = 0;
    int sum_abs_acc = 0;
    int max_acc = -2147483647;
    int even_dot_acc = 0;

    for (int i = 0; i < n; i++) {
        int av = a[i];
        int bv = b[i];
        int prod = av * bv;
        int abs_av = (av < 0) ? -av : av;
        dot_acc += av * bv;
        sum_acc += av;
        sum_abs_acc += abs_av;
        if (av > max_acc) {
            max_acc = av;
        }
        if ((i & 1) == 0) {
            even_dot_acc += prod;
        }
    }

    result[0] = dot_acc;
    result[1] = sum_acc;
    result[2] = sum_abs_acc;
    result[3] = max_acc;
    result[4] = even_dot_acc;
}
