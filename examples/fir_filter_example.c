#define FIR_EXAMPLE_TAPS 8
#define FIR_EXAMPLE_MAX_N 256

void fir_filter_example(
    const int input[FIR_EXAMPLE_MAX_N],
    const int coefficients[FIR_EXAMPLE_TAPS],
    int filtered[FIR_EXAMPLE_MAX_N],
    int moving_average[FIR_EXAMPLE_MAX_N],
    int n
) {
    int delay[FIR_EXAMPLE_TAPS];

    if (n > FIR_EXAMPLE_MAX_N) {
        n = FIR_EXAMPLE_MAX_N;
    }

    for (int tap = 0; tap < FIR_EXAMPLE_TAPS; tap++) {
        delay[tap] = 0;
    }

    for (int sample = 0; sample < n; sample++) {
        int accumulator = 0;
        int average_sum = 0;

        for (int tap = FIR_EXAMPLE_TAPS - 1; tap > 0; tap--) {
            delay[tap] = delay[tap - 1];
        }
        delay[0] = input[sample];

        for (int tap = 0; tap < FIR_EXAMPLE_TAPS; tap++) {
            accumulator += delay[tap] * coefficients[tap];
        }
        for (int window = 0; window < 4; window++) {
            average_sum += delay[window];
        }

        filtered[sample] = accumulator;
        moving_average[sample] = average_sum / 4;
    }
}
