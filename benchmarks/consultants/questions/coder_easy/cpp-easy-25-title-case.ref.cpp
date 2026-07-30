#include <iostream>
#include <string>
#include <cctype>
int main() {
    std::string w;
    bool first = true;
    while (std::cin >> w) {
        if (!first) std::cout << ' ';
        first = false;
        for (size_t i = 0; i < w.size(); i++) {
            unsigned char c = (unsigned char)w[i];
            std::cout << (char)(i == 0 ? std::toupper(c) : std::tolower(c));
        }
    }
    std::cout << std::endl;
    return 0;
}
