#include <iostream>
#include <string>
int main() {
    unsigned long long n;
    std::cin >> n;
    if (n == 0) { std::cout << "0" << std::endl; return 0; }
    std::string out;
    while (n > 0) { out += char('0' + (n & 1)); n >>= 1; }
    std::string rev(out.rbegin(), out.rend());
    std::cout << rev << std::endl;
    return 0;
}
