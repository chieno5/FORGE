#define SENSOR_BLOCK_COUNT 128
#define READINGS_PER_BLOCK 16
#define TOTAL_SENSOR_READINGS (SENSOR_BLOCK_COUNT * READINGS_PER_BLOCK)

void sum_sensor_blocks(
    const int sensor_readings[TOTAL_SENSOR_READINGS],
    long long total_output[1]
) {
    long long total = 0;

    for (int block = 0; block < SENSOR_BLOCK_COUNT; block++) {
        int offset = block * READINGS_PER_BLOCK;
        total +=
            sensor_readings[offset] + sensor_readings[offset + 1] +
            sensor_readings[offset + 2] + sensor_readings[offset + 3] +
            sensor_readings[offset + 4] + sensor_readings[offset + 5] +
            sensor_readings[offset + 6] + sensor_readings[offset + 7] +
            sensor_readings[offset + 8] + sensor_readings[offset + 9] +
            sensor_readings[offset + 10] + sensor_readings[offset + 11] +
            sensor_readings[offset + 12] + sensor_readings[offset + 13] +
            sensor_readings[offset + 14] + sensor_readings[offset + 15];
    }

    total_output[0] = total;
}
