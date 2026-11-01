// A virtual pointer that stays plugged in, for driving a headless sway. wlrctl makes a new
// device per command, so apps never bind wl_pointer before it is gone again.
// stdin: "move X Y" (absolute pixels on the output), "click left|right", "quit".
#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>

#include "vp.h"

static struct wl_seat *seat;
static struct zwlr_virtual_pointer_manager_v1 *manager;

static void global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t version) {
    if (strcmp(iface, wl_seat_interface.name) == 0)
        seat = wl_registry_bind(reg, name, &wl_seat_interface, 1);
    else if (strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name) == 0)
        manager = wl_registry_bind(reg, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
}

static void global_remove(void *data, struct wl_registry *reg, uint32_t name) {}

static const struct wl_registry_listener listener = {global, global_remove};

static uint32_t now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: vpointer WIDTH HEIGHT\n");
        return 2;
    }
    uint32_t w = atoi(argv[1]), h = atoi(argv[2]);
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        fprintf(stderr, "vpointer: no wayland display\n");
        return 1;
    }
    struct wl_registry *reg = wl_display_get_registry(display);
    wl_registry_add_listener(reg, &listener, NULL);
    wl_display_roundtrip(display);
    if (!seat || !manager) {
        fprintf(stderr, "vpointer: compositor lacks a seat or the virtual pointer protocol\n");
        return 1;
    }
    struct zwlr_virtual_pointer_v1 *p = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, seat);
    wl_display_roundtrip(display);

    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        int x, y;
        char which[16];
        if (sscanf(line, "move %d %d", &x, &y) == 2) {
            zwlr_virtual_pointer_v1_motion_absolute(p, now(), x, y, w, h);
            zwlr_virtual_pointer_v1_frame(p);
        } else if (sscanf(line, "click %15s", which) == 1) {
            uint32_t b = strcmp(which, "right") == 0 ? BTN_RIGHT : BTN_LEFT;
            zwlr_virtual_pointer_v1_button(p, now(), b, WL_POINTER_BUTTON_STATE_PRESSED);
            zwlr_virtual_pointer_v1_frame(p);
            wl_display_roundtrip(display);
            zwlr_virtual_pointer_v1_button(p, now(), b, WL_POINTER_BUTTON_STATE_RELEASED);
            zwlr_virtual_pointer_v1_frame(p);
        } else if (strncmp(line, "quit", 4) == 0) {
            break;
        }
        wl_display_roundtrip(display);
    }
    zwlr_virtual_pointer_v1_destroy(p);
    wl_display_roundtrip(display);
    wl_display_disconnect(display);
    return 0;
}
