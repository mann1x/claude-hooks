#include <iostream>
#include <string>
int main() {
    std::string tok;
    std::cin >> tok;
    long sum = 0;
    for (char c : tok)
        if (c >= '0' && c <= '9') sum += c - '0';
    std::cout << sum << std::endl;
    return 0;
}
