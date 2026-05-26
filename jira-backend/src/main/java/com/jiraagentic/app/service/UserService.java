package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CreateUserRequest;
import com.jiraagentic.app.entity.User;
import com.jiraagentic.app.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.cache.annotation.CacheEvict;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Locale;
import java.util.Optional;

@Service
@RequiredArgsConstructor
@Slf4j
public class UserService {

    private final UserRepository userRepository;

    public List<User> findAll() {
        return userRepository.findAll();
    }

    @Cacheable(value = "users", key = "#id")
    public User findById(Long id) {
        log.info("DB lookup user id={}", id);
        return userRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("User not found: " + id));
    }

    public User findByUsername(String username) {
        return userRepository.findByUsername(username)
                .orElseThrow(() -> new RuntimeException("User not found: " + username));
    }

    public User create(CreateUserRequest req) {
        User user = new User();
        user.setUsername(req.getUsername());
        user.setName(req.getName());
        user.setEmail(req.getEmail());
        user.setPassword("123");
        user.setAvatarColor(req.getAvatarColor());
        return userRepository.save(user);
    }

    public User authenticate(String username, String password) {
        User user = userRepository.findByUsername(username)
                .orElseThrow(() -> new RuntimeException("User not found: " + username));
        if (user.getPassword() == null) {
            throw new RuntimeException("This account uses GitHub sign-in");
        }
        if (!user.getPassword().equals(password)) {
            throw new RuntimeException("Invalid password");
        }
        return user;
    }

    /**
     * Find or create a user after GitHub OAuth. Links {@code githubId} to an existing row by email when needed.
     */
    @Transactional
    @CacheEvict(value = "users", allEntries = true)
    public User upsertFromGithub(String githubId, String email, String fullName) {
        Optional<User> bySub = userRepository.findByGithubId(githubId);
        if (bySub.isPresent()) {
            User u = bySub.get();
            u.setEmail(email);
            u.setName(fullName);
            return userRepository.save(u);
        }
        Optional<User> byEmail = userRepository.findByEmail(email);
        if (byEmail.isPresent()) {
            User u = byEmail.get();
            u.setGithubId(githubId);
            u.setName(fullName);
            // Keep username/password login working for accounts that already had a password (e.g. seeded users).
            if (u.getPassword() == null && isSeedDemoEmail(u.getEmail())) {
                u.setPassword("123");
            }
            return userRepository.save(u);
        }
        User created = new User();
        created.setUsername(generateUniqueUsernameFromEmail(email));
        created.setEmail(email);
        created.setName(fullName);
        created.setPassword(null);
        created.setGithubId(githubId);
        created.setAvatarColor("linear-gradient(135deg, #24292f, #57606a)");
        return userRepository.save(created);
    }

    private static boolean isSeedDemoEmail(String email) {
        return email != null
                && (email.equalsIgnoreCase("alice@example.com")
                || email.equalsIgnoreCase("john@example.com")
                || email.equalsIgnoreCase("charles@example.com"));
    }

    private String generateUniqueUsernameFromEmail(String email) {
        String local = email.split("@")[0].toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9._-]", "");
        if (local.length() > 40) {
            local = local.substring(0, 40);
        }
        if (local.isEmpty()) {
            local = "user";
        }
        String candidate = local;
        int i = 0;
        while (userRepository.existsByUsername(candidate)) {
            i++;
            String suffix = String.valueOf(i);
            candidate = local.substring(0, Math.max(1, 50 - suffix.length() - 1)) + "_" + suffix;
        }
        return candidate;
    }

    @CacheEvict(value = "users", key = "#id")
    public void delete(Long id) {
        if (!userRepository.existsById(id)) {
            throw new RuntimeException("User not found: " + id);
        }
        userRepository.deleteById(id);
    }
}
