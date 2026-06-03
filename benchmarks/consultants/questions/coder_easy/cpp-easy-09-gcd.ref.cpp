#include <iostream>
int main() {
    long long a, b;
    std::cin >> a >> b;
    while (b) { long long t = b; b = a % b; a = t; }
    std::cout << a << std::endl;
    return 0;
}
