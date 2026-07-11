#include "vector_saxpy.h"

void vector_saxpy(
    const int a[VECTOR_SAXPY_MAX_N],
    const int b[VECTOR_SAXPY_MAX_N],
    const int c[VECTOR_SAXPY_MAX_N],
    int add_out[VECTOR_SAXPY_MAX_N],
    int saxpy_out[VECTOR_SAXPY_MAX_N],
    int triad_out[VECTOR_SAXPY_MAX_N],
    int blend_out[VECTOR_SAXPY_MAX_N],
    int alpha,
    int beta,
    int n
) {
#pragma HLS INTERFACE m_axi port=a offset=slave bundle=gmem0
#pragma HLS INTERFACE m_axi port=b offset=slave bundle=gmem1
#pragma HLS INTERFACE m_axi port=c offset=slave bundle=gmem2
#pragma HLS INTERFACE m_axi port=add_out offset=slave bundle=gmem3
#pragma HLS INTERFACE m_axi port=saxpy_out offset=slave bundle=gmem4
#pragma HLS INTERFACE m_axi port=triad_out offset=slave bundle=gmem5
#pragma HLS INTERFACE m_axi port=blend_out offset=slave bundle=gmem6
#pragma HLS INTERFACE s_axilite port=a bundle=CTRL
#pragma HLS INTERFACE s_axilite port=b bundle=CTRL
#pragma HLS INTERFACE s_axilite port=c bundle=CTRL
#pragma HLS INTERFACE s_axilite port=add_out bundle=CTRL
#pragma HLS INTERFACE s_axilite port=saxpy_out bundle=CTRL
#pragma HLS INTERFACE s_axilite port=triad_out bundle=CTRL
#pragma HLS INTERFACE s_axilite port=blend_out bundle=CTRL
#pragma HLS INTERFACE s_axilite port=alpha bundle=CTRL
#pragma HLS INTERFACE s_axilite port=beta bundle=CTRL
#pragma HLS INTERFACE s_axilite port=n bundle=CTRL
#pragma HLS INTERFACE s_axilite port=return bundle=CTRL

    if (n > VECTOR_SAXPY_MAX_N) {
        n = VECTOR_SAXPY_MAX_N;
    }

    for (int i = 0; i < n; i++) {
        int av = a[i];
        int bv = b[i];
        int cv = c[i];
        int add_v = av + bv;
        int saxpy_v = alpha * av + bv;
        int triad_v = av + beta * bv + cv;

        add_out[i] = add_v;
        saxpy_out[i] = saxpy_v;
        triad_out[i] = triad_v;
        blend_out[i] = add_v + saxpy_v - triad_v;
    }

    for (int i = 1; i < n - 1; i++) {
        int left = add_out[i - 1];
        int mid = add_out[i];
        int right = add_out[i + 1];
        blend_out[i] += left - 2 * mid + right;
    }

    if (n > 0) {
        blend_out[0] += add_out[0];
        blend_out[n - 1] += add_out[n - 1];
    }
}
