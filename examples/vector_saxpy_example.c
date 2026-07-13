#include "vector_saxpy_example.h"

void vector_saxpy_example(
    const int a[VECTOR_EXAMPLE_MAX_N],
    const int b[VECTOR_EXAMPLE_MAX_N],
    int result[VECTOR_EXAMPLE_MAX_N],
    int alpha,
    int n
) {
    if (n > VECTOR_EXAMPLE_MAX_N) {
        n = VECTOR_EXAMPLE_MAX_N;
    }

    for (int i = 0; i < n; i++) {
        result[i] = alpha * a[i] + b[i];
    }

    for (int i = 1; i < n - 1; i++) {
        int left = result[i - 1];
        int center = result[i];
        int right = result[i + 1];
        result[i] = left + 2 * center + right;
    }
}
