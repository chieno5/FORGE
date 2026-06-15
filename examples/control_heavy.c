int control_heavy(int mode, int value) {
    if (mode == 0) {
        printf("mode zero\n");
        return value;
    }

    if (mode == 1) {
        return value > 10 ? value - 1 : value + 1;
    }

    if (mode == 2) {
        return value == 0 ? 0 : 100 / value;
    }

    return -1;
}
