#pragma once

#define VECTOR_EXAMPLE_MAX_N 256

void vector_saxpy_example(
    const int a[VECTOR_EXAMPLE_MAX_N],
    const int b[VECTOR_EXAMPLE_MAX_N],
    int result[VECTOR_EXAMPLE_MAX_N],
    int alpha,
    int n
);
