#define MATRIX_EXAMPLE_N 16

void matrix_multiply_example(
    const int left[MATRIX_EXAMPLE_N][MATRIX_EXAMPLE_N],
    const int right[MATRIX_EXAMPLE_N][MATRIX_EXAMPLE_N],
    const int bias[MATRIX_EXAMPLE_N],
    int output[MATRIX_EXAMPLE_N][MATRIX_EXAMPLE_N],
    int row_sum[MATRIX_EXAMPLE_N]
) {
    for (int row = 0; row < MATRIX_EXAMPLE_N; row++) {
        int sum = 0;

        for (int column = 0; column < MATRIX_EXAMPLE_N; column++) {
            int accumulator = bias[column];

            for (int inner = 0; inner < MATRIX_EXAMPLE_N; inner++) {
                accumulator += left[row][inner] * right[inner][column];
            }

            output[row][column] = accumulator;
            sum += accumulator;
        }

        row_sum[row] = sum;
    }
}
