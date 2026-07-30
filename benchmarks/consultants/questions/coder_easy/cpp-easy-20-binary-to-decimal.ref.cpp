#include <iostream>
#include <string>
int main() {
    std::string tok;
    std::cin >> tok;
    long long v = 0;
    for (char c : tok) v = v * 2 + (c - '0');
    std::cout << v << std::endl;
    return 0;
}
