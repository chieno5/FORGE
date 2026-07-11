#pragma once

#define VECTOR_SAXPY_MAX_N 512

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
);
