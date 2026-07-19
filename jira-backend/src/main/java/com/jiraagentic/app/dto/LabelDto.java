package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.Label;
import lombok.Data;

@Data
public class LabelDto {
    private Long id;
    private Long spaceId;
    private String name;

    public static LabelDto from(Label label) {
        LabelDto dto = new LabelDto();
        dto.setId(label.getId());
        dto.setSpaceId(label.getSpace().getId());
        dto.setName(label.getName());
        return dto;
    }
}
