package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class ReorderSprintRequest {
    /**
     * move_up | move_down | move_to_top | move_to_bottom
     * Only future (planned) sprints can be reordered.
     */
    private String action;
}
