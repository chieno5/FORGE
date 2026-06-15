typedef unsigned char pixel_t;

int clamp_to_byte(int value) {
    if (value < 0) {
        return 0;
    }
    if (value > 255) {
        return 255;
    }
    return value;
}

void rgb_to_luma(
    const pixel_t red[65536],
    const pixel_t green[65536],
    const pixel_t blue[65536],
    pixel_t gray[65536],
    int pixels
) {
    for (int i = 0; i < pixels; i++) {
        int weighted = 77 * red[i] + 150 * green[i] + 29 * blue[i];
        gray[i] = weighted >> 8;
    }
}

void box_blur_3x3(
    const pixel_t input[65536],
    pixel_t blurred[65536],
    int width,
    int height
) {
    for (int y = 1; y < height - 1; y++) {
        for (int x = 1; x < width - 1; x++) {
            int sum = 0;

            for (int ky = -1; ky <= 1; ky++) {
                for (int kx = -1; kx <= 1; kx++) {
                    int yy = y + ky;
                    int xx = x + kx;
                    sum += input[yy * width + xx];
                }
            }

            blurred[y * width + x] = sum / 9;
        }
    }
}

void sobel_edges(
    const pixel_t input[65536],
    pixel_t edges[65536],
    int width,
    int height
) {
    const int kernel_x[3][3] = {
        {-1, 0, 1},
        {-2, 0, 2},
        {-1, 0, 1}
    };
    const int kernel_y[3][3] = {
        {-1, -2, -1},
        {0, 0, 0},
        {1, 2, 1}
    };

    for (int y = 1; y < height - 1; y++) {
        for (int x = 1; x < width - 1; x++) {
            int grad_x = 0;
            int grad_y = 0;

            for (int ky = 0; ky < 3; ky++) {
                for (int kx = 0; kx < 3; kx++) {
                    int yy = y + ky - 1;
                    int xx = x + kx - 1;
                    int pixel = input[yy * width + xx];
                    grad_x += pixel * kernel_x[ky][kx];
                    grad_y += pixel * kernel_y[ky][kx];
                }
            }

            if (grad_x < 0) {
                grad_x = -grad_x;
            }
            if (grad_y < 0) {
                grad_y = -grad_y;
            }

            edges[y * width + x] = clamp_to_byte(grad_x + grad_y);
        }
    }
}

void threshold_edges(
    const pixel_t edges[65536],
    pixel_t mask[65536],
    int pixels,
    int threshold
) {
    for (int i = 0; i < pixels; i++) {
        if (edges[i] > threshold) {
            mask[i] = 255;
        } else {
            mask[i] = 0;
        }
    }
}

int count_defect_pixels(const pixel_t mask[65536], int pixels) {
    int count = 0;

    for (int i = 0; i < pixels; i++) {
        if (mask[i] != 0) {
            count += 1;
        }
    }

    return count;
}

int inspect_frame(
    const pixel_t red[65536],
    const pixel_t green[65536],
    const pixel_t blue[65536],
    pixel_t gray[65536],
    pixel_t blurred[65536],
    pixel_t edges[65536],
    pixel_t mask[65536],
    int width,
    int height,
    int edge_threshold,
    int max_allowed_defects
) {
    int pixels = width * height;

    if (width < 3 || height < 3) {
        return -1;
    }
    if (pixels > 65536) {
        return -2;
    }

    rgb_to_luma(red, green, blue, gray, pixels);
    box_blur_3x3(gray, blurred, width, height);
    sobel_edges(blurred, edges, width, height);
    threshold_edges(edges, mask, pixels, edge_threshold);

    int defect_pixels = count_defect_pixels(mask, pixels);
    if (defect_pixels > max_allowed_defects) {
        return 1;
    }

    return 0;
}
