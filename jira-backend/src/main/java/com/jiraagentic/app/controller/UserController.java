package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateUserRequest;
import com.jiraagentic.app.dto.UserDto;
import com.jiraagentic.app.service.UserService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.stream.Collectors;

@RestController
@RequestMapping("/api/users")
@RequiredArgsConstructor
public class UserController {

    private final UserService userService;

    @GetMapping
    public List<UserDto> getAll() {
        return userService.findAll().stream()
                .map(UserDto::from)
                .collect(Collectors.toList());
    }

    @GetMapping("/{id}")
    public UserDto getById(@PathVariable Long id) {
        return UserDto.from(userService.findById(id));
    }

    @PostMapping
    public UserDto create(@RequestBody CreateUserRequest req) {
        return UserDto.from(userService.create(req));
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        userService.delete(id);
        return ResponseEntity.noContent().build();
    }
}
