package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.Space;
import lombok.Data;
import java.util.List;

@Data
public class SpaceDto {
    private Long id;
    private String name;
    private String key;
    private String color;
    private Long ownerId;
    private List<UserDto> members;
    private List<GroupDto> groups;

    public static SpaceDto from(Space s) {
        SpaceDto dto = new SpaceDto();
        dto.setId(s.getId());
        dto.setName(s.getName());
        dto.setKey(s.getKey());
        dto.setColor(s.getColor());
        if (s.getOwner() != null) dto.setOwnerId(s.getOwner().getId());
        return dto;
    }
}
