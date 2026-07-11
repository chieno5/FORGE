#pragma once

#define REDUCTION_MAX_N 1024

void reduction_dot(
    const int a[REDUCTION_MAX_N],
    const int b[REDUCTION_MAX_N],
    int result[5],
    int n
);
