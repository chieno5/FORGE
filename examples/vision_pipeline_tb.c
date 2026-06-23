typedef unsigned char pixel_t;

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
);

static pixel_t red[65536];
static pixel_t green[65536];
static pixel_t blue[65536];
static pixel_t gray[65536];
static pixel_t blurred[65536];
static pixel_t edges[65536];
static pixel_t mask[65536];

int main(void) {
    const int width = 16;
    const int height = 16;
    const int pixels = width * height;

    for (int i = 0; i < pixels; i++) {
        red[i] = (pixel_t)(i & 255);
        green[i] = (pixel_t)((i * 3) & 255);
        blue[i] = (pixel_t)((i * 7) & 255);
    }

    int result = inspect_frame(
        red,
        green,
        blue,
        gray,
        blurred,
        edges,
        mask,
        width,
        height,
        80,
        pixels
    );
    return result == 0 ? 0 : 1;
}
