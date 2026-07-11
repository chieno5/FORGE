#include <stdio.h>
#include "vector_saxpy.h"

int main() {
    const int n = 257;
    const int alpha = 3;
    const int beta = -2;
    int a[VECTOR_SAXPY_MAX_N];
    int b[VECTOR_SAXPY_MAX_N];
    int c[VECTOR_SAXPY_MAX_N];
    int add_out[VECTOR_SAXPY_MAX_N];
    int saxpy_out[VECTOR_SAXPY_MAX_N];
    int triad_out[VECTOR_SAXPY_MAX_N];
    int blend_out[VECTOR_SAXPY_MAX_N];

    for (int i = 0; i < VECTOR_SAXPY_MAX_N; i++) {
        a[i] = (i * 7 + 1) % 97;
        b[i] = (i * 5 - 3) % 89;
        c[i] = (i * 13 + 11) % 101;
        add_out[i] = 0;
        saxpy_out[i] = 0;
        triad_out[i] = 0;
        blend_out[i] = 0;
    }

    vector_saxpy(a, b, c, add_out, saxpy_out, triad_out, blend_out, alpha, beta, n);

    int errors = 0;
    for (int i = 0; i < n; i++) {
        int add_ref = a[i] + b[i];
        int saxpy_ref = alpha * a[i] + b[i];
        int triad_ref = a[i] + beta * b[i] + c[i];
        int blend_ref = add_ref + saxpy_ref - triad_ref;
        if (i > 0 && i < n - 1) {
            blend_ref += (a[i - 1] + b[i - 1]) - 2 * add_ref + (a[i + 1] + b[i + 1]);
        } else {
            blend_ref += add_ref;
        }
        if (add_out[i] != add_ref ||
            saxpy_out[i] != saxpy_ref ||
            triad_out[i] != triad_ref ||
            blend_out[i] != blend_ref) {
            errors++;
        }
    }

    if (errors == 0) {
        printf("PASS\n");
        return 0;
    }
    printf("FAIL errors=%d\n", errors);
    return 1;
}
